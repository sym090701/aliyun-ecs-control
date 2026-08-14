from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import math
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as datetime_time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - exercised on Python 3.8 deployments
    from backports.zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aliyunsdkcore.client import AcsClient
from aliyunsdkcore.request import CommonRequest
from aliyunsdkecs.request.v20140526 import (
    DescribeInstancesRequest,
    StartInstancesRequest,
    StopInstancesRequest,
)

from .config import (
    AccountConfig,
    Config,
    ManagedConfigStore,
    TargetConfig,
    _identifier,
    _nonempty,
    _timezone_value,
)
from .config import UTC_OFFSET_PATTERN

LOG = logging.getLogger(__name__)
ACTIVITY_HISTORY_DAYS = 5
ACTIVITY_SOURCES = {"all", "traffic", "schedule", "manual", "config"}
ACTIVITY_OUTCOMES = {"all", "success", "skipped", "failed"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_name(f"{path.name}.lock")
        self._thread_lock = threading.Lock()

    def load_all(self) -> Dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
                if isinstance(payload, dict) and isinstance(payload.get("targets"), dict):
                    return payload
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            LOG.warning("Could not read state file: %s", exc)
        return {"targets": {}}

    def target(self, target: TargetConfig) -> Dict[str, Any]:
        defaults = self._defaults(target)
        stored = self.load_all().get("targets", {}).get(target.id, {})
        if isinstance(stored, dict):
            defaults.update(stored)
        if not isinstance(defaults.get("activity_history"), list):
            defaults["activity_history"] = []
        defaults["schedule_days"] = EcsController.schedule_days(defaults)
        return defaults

    @staticmethod
    def _defaults(target: TargetConfig) -> Dict[str, Any]:
        return {
            "auto_enabled": target.auto_enabled_default,
            "schedule_enabled": target.schedule_enabled_default,
            "schedule_start": target.schedule_start_default,
            "schedule_stop": target.schedule_stop_default,
            "schedule_weekdays": list(target.schedule_weekdays_default),
            "schedule_timezone": target.schedule_timezone_default,
            "last_schedule_event": None,
            "last_check": None,
            "last_action": None,
            "last_result": None,
            "last_traffic_gb": None,
            "activity_history": [],
        }

    def _target_from_payload(self, target: TargetConfig, payload: Dict[str, Any]) -> Dict[str, Any]:
        state = self._defaults(target)
        stored = payload.get("targets", {}).get(target.id, {})
        if isinstance(stored, dict):
            state.update(stored)
        if not isinstance(state.get("activity_history"), list):
            state["activity_history"] = []
        state["schedule_days"] = EcsController.schedule_days(state)
        return state

    @staticmethod
    def _recent_history(history: object) -> List[Dict[str, Any]]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=ACTIVITY_HISTORY_DAYS)
        kept: List[Dict[str, Any]] = []
        for item in history if isinstance(history, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                at = datetime.fromisoformat(str(item.get("at", "")).replace("Z", "+00:00"))
            except ValueError:
                continue
            if at.tzinfo is not None and at.astimezone(timezone.utc) >= cutoff:
                kept.append(item)
        return kept

    def update_target(self, target: TargetConfig, **changes: Any) -> Dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock:
            with self.lock_path.open("a", encoding="utf-8") as lock_handle:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                payload = self.load_all()
                current = self._target_from_payload(target, payload)
                current.update(changes)
                payload.setdefault("targets", {})[target.id] = current
                self._save(payload)
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                return current

    def record_activity(
        self,
        target: TargetConfig,
        action: str,
        result: str,
        outcome: str = "success",
        **changes: Any,
    ) -> Dict[str, Any]:
        if outcome not in {"success", "skipped", "failed"}:
            raise ValueError("activity outcome is invalid")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        event = {"at": utc_now(), "action": action, "result": result, "outcome": outcome}
        with self._thread_lock:
            with self.lock_path.open("a", encoding="utf-8") as lock_handle:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                payload = self.load_all()
                current = self._target_from_payload(target, payload)
                history = self._recent_history(current["activity_history"])
                current.update(changes)
                current.update(last_check=event["at"], last_action=action, last_result=result)
                current["activity_history"] = history + [event]
                payload.setdefault("targets", {})[target.id] = current
                self._save(payload)
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                return current

    def remove_target(self, target_id: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock:
            with self.lock_path.open("a", encoding="utf-8") as lock_handle:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                payload = self.load_all()
                payload.setdefault("targets", {}).pop(target_id, None)
                self._save(payload)
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def _save(self, payload: Dict[str, Any]) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix="state-", dir=str(self.path.parent))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)


class EcsController:
    def __init__(
        self,
        config: Config,
        target: TargetConfig,
        account: AccountConfig,
        state: StateStore,
        client: Optional[AcsClient] = None,
    ) -> None:
        self.config = config
        self.target = target
        self.account = account
        self.state = state
        self.client = client or AcsClient(
            account.access_key_id,
            account.access_key_secret,
            target.region_id,
        )
        if hasattr(self.client, "set_connect_timeout"):
            self.client.set_connect_timeout(5)
        if hasattr(self.client, "set_read_timeout"):
            self.client.set_read_timeout(20)

    @contextlib.contextmanager
    def exclusive(self) -> Iterator[None]:
        self.config.lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.config.lock_dir / f"{self.target.id}.lock"
        with lock_path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _call(self, request: Any) -> Dict[str, Any]:
        response = self.client.do_action_with_exception(request)
        return json.loads(response.decode("utf-8"))

    def get_status(self) -> str:
        request = DescribeInstancesRequest.DescribeInstancesRequest()
        request.set_InstanceIds([self.target.instance_id])
        payload = self._call(request)
        instances = payload.get("Instances", {}).get("Instance", [])
        if not instances:
            raise RuntimeError(f"ECS instance was not found: {self.target.name}")
        return str(instances[0].get("Status", "Unknown"))

    def get_traffic_gb(self) -> float:
        request = CommonRequest()
        request.set_domain("cdt.aliyuncs.com")
        request.set_version("2021-08-13")
        request.set_action_name("ListCdtInternetTraffic")
        request.set_method("POST")
        payload = self._call(request)
        details = payload.get("TrafficDetails", [])
        if not isinstance(details, list):
            raise RuntimeError("Unexpected CDT response: TrafficDetails is not a list")
        total_bytes = sum(float(item.get("Traffic", 0) or 0) for item in details)
        return total_bytes / (1024**3)

    def metadata(self) -> Dict[str, Any]:
        return {
            "id": self.target.id,
            "name": self.target.name,
            "account": self.account.name,
            "region": self.target.region_id,
            "instance": self.target.instance_label,
            "threshold_gb": self.target.traffic_threshold_gb,
        }

    def overview(self, traffic_gb: Optional[float] = None) -> Dict[str, Any]:
        status = self.get_status()
        traffic_error = None
        if traffic_gb is None:
            try:
                traffic = self.get_traffic_gb()
            except Exception as exc:
                LOG.warning("Traffic query failed for %s: %s", self.target.id, exc)
                traffic = None
                traffic_error = str(exc)
        else:
            traffic = traffic_gb
        state = self.state.target(self.target)
        next_event = self.next_schedule_event(state=state)
        return {
            **self.metadata(),
            "status": status,
            "traffic_gb": round(traffic, 2) if traffic is not None else None,
            "usage_percent": round(min(traffic / self.target.traffic_threshold_gb * 100, 100), 1)
            if traffic is not None
            else 0,
            "traffic_error": traffic_error,
            "schedule_next_at": next_event["at"] if next_event else None,
            "schedule_next_action": next_event["action"] if next_event else None,
            **state,
            "activity_history": list(reversed(state.get("activity_history", [])[-10:])),
        }

    def _record(self, action: str, result: str, outcome: str = "success", **extra: Any) -> None:
        self.state.record_activity(
            self.target,
            action,
            result,
            outcome,
            **extra,
        )

    def record_failure(self, action: str, error: Exception) -> None:
        self._record(action, str(error), "failed")

    def _ensure_power_locked(self, decision: str) -> Dict[str, Any]:
        before = self.get_status()
        if decision == "start":
            if before in {"Running", "Starting"}:
                return {"changed": False, "status": before, "message": "Instance is already running"}
            request = StartInstancesRequest.StartInstancesRequest()
            request.set_InstanceIds([self.target.instance_id])
            request.set_accept_format("json")
            self._call(request)
            return {"changed": True, "status": "Starting", "message": "Start request accepted"}
        if decision == "stop":
            if before in {"Stopped", "Stopping"}:
                return {"changed": False, "status": before, "message": "Instance is already stopped"}
            request = StopInstancesRequest.StopInstancesRequest()
            request.set_InstanceIds([self.target.instance_id])
            request.set_ForceStop(False)
            request.set_accept_format("json")
            self._call(request)
            return {"changed": True, "status": "Stopping", "message": "Graceful stop request accepted"}
        raise ValueError(f"Unknown power decision: {decision}")

    def start(self) -> Dict[str, Any]:
        with self.exclusive():
            try:
                result = self._ensure_power_locked("start")
                self._record("start", result["message"])
                return {"target_id": self.target.id, **result}
            except Exception as exc:
                self._record("start", str(exc), "failed")
                raise

    def stop(self) -> Dict[str, Any]:
        with self.exclusive():
            try:
                result = self._ensure_power_locked("stop")
                self._record("stop", result["message"])
                return {"target_id": self.target.id, **result}
            except Exception as exc:
                self._record("stop", str(exc), "failed")
                raise

    def set_auto(self, enabled: bool) -> Dict[str, Any]:
        with self.exclusive():
            return self.state.record_activity(
                self.target,
                "auto_enabled" if enabled else "auto_disabled",
                "Automatic traffic policy enabled" if enabled else "Automatic traffic policy paused",
                auto_enabled=enabled,
            )

    @staticmethod
    def _schedule_timezone(value: object) -> tzinfo:
        name = str(value or "").strip()
        if not name:
            raise ValueError("schedule timezone cannot be empty")
        match = UTC_OFFSET_PATTERN.fullmatch(name)
        if match:
            hours, minutes = int(match.group(2)), int(match.group(3))
            if hours == 14 and minutes:
                raise ValueError("schedule UTC offset must not exceed 14 hours")
            offset = timedelta(hours=hours, minutes=minutes)
            if match.group(1) == "-":
                offset = -offset
            return timezone(offset, name)
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown schedule timezone: {name}") from exc

    @staticmethod
    def _schedule_time(value: object, field: str) -> str:
        text = str(value or "").strip()
        try:
            parsed = datetime.strptime(text, "%H:%M")
        except ValueError as exc:
            raise ValueError(f"{field} must use 24-hour HH:MM format") from exc
        if parsed.strftime("%H:%M") != text:
            raise ValueError(f"{field} must use 24-hour HH:MM format")
        return text

    @staticmethod
    def _schedule_weekdays(value: object) -> List[int]:
        if not isinstance(value, list) or not value:
            raise ValueError("schedule weekdays must be a non-empty array")
        if any(isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6 for day in value):
            raise ValueError("schedule weekdays must contain integers between 0 and 6")
        if len(set(value)) != len(value):
            raise ValueError("schedule weekdays cannot contain duplicates")
        return sorted(value)

    @staticmethod
    def _schedule_weekday_set(value: object) -> Set[int]:
        """Read persisted legacy weekdays, where an empty list means no active day."""
        if not isinstance(value, list):
            raise ValueError("schedule weekdays must be an array")
        if any(isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6 for day in value):
            raise ValueError("schedule weekdays must contain integers between 0 and 6")
        if len(set(value)) != len(value):
            raise ValueError("schedule weekdays cannot contain duplicates")
        return set(value)

    @classmethod
    def schedule_days(cls, state: Dict[str, Any]) -> List[Dict[str, Any]]:
        fallback_enabled = cls._schedule_weekday_set(state.get("schedule_weekdays", list(range(7))))
        fallback_start = cls._schedule_time(state.get("schedule_start", "08:00"), "schedule start")
        fallback_stop = cls._schedule_time(state.get("schedule_stop", "23:00"), "schedule stop")
        rows = state.get("schedule_days")
        parsed: Dict[int, Dict[str, Any]] = {}
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                day = row.get("weekday")
                if isinstance(day, int) and not isinstance(day, bool) and 0 <= day <= 6 and day not in parsed:
                    parsed[day] = {
                        "weekday": day,
                        "enabled": bool(row.get("enabled", False)),
                        "start": cls._schedule_time(row.get("start", fallback_start), "schedule start"),
                        "stop": cls._schedule_time(row.get("stop", fallback_stop), "schedule stop"),
                    }
        return [
            parsed.get(day, {"weekday": day, "enabled": day in fallback_enabled, "start": fallback_start, "stop": fallback_stop})
            for day in range(7)
        ]

    @classmethod
    def _validate_schedule_days(cls, value: object, state: Dict[str, Any]) -> List[Dict[str, Any]]:
        if value is None:
            return cls.schedule_days(state)
        if not isinstance(value, list) or len(value) != 7:
            raise ValueError("schedule days must contain all seven weekdays")
        rows: Dict[int, Dict[str, Any]] = {}
        for row in value:
            if not isinstance(row, dict):
                raise ValueError("schedule day must be an object")
            day = row.get("weekday")
            if isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6 or day in rows:
                raise ValueError("schedule day weekday is invalid")
            enabled = row.get("enabled")
            if not isinstance(enabled, bool):
                raise ValueError("schedule day enabled must be a boolean")
            start = cls._schedule_time(row.get("start"), "schedule start")
            stop = cls._schedule_time(row.get("stop"), "schedule stop")
            if start == stop:
                raise ValueError("schedule start and stop times must differ")
            rows[day] = {"weekday": day, "enabled": enabled, "start": start, "stop": stop}
        if set(rows) != set(range(7)):
            raise ValueError("schedule days must contain all seven weekdays")
        return [rows[day] for day in range(7)]

    def set_schedule(
        self,
        enabled: bool,
        start_time: Optional[object] = None,
        stop_time: Optional[object] = None,
        weekdays: Optional[object] = None,
        timezone_name: Optional[object] = None,
        days: Optional[object] = None,
    ) -> Dict[str, Any]:
        if not isinstance(enabled, bool):
            raise ValueError("schedule enabled must be a boolean")
        with self.exclusive():
            state = self.state.target(self.target)
            if days is None:
                start = self._schedule_time(state["schedule_start"] if start_time is None else start_time, "schedule start")
                stop = self._schedule_time(state["schedule_stop"] if stop_time is None else stop_time, "schedule stop")
                if start == stop:
                    raise ValueError("schedule start and stop times must differ")
                selected_days = sorted(self._schedule_weekday_set(
                    state["schedule_weekdays"] if weekdays is None else weekdays
                ))
                if enabled and not selected_days:
                    raise ValueError("schedule weekdays must be a non-empty array")
                schedule_days = [
                    {"weekday": day, "enabled": day in selected_days, "start": start, "stop": stop}
                    for day in range(7)
                ]
            else:
                schedule_days = self._validate_schedule_days(days, state)
                enabled_days = [row for row in schedule_days if row["enabled"]]
                if enabled and not enabled_days:
                    raise ValueError("enable at least one schedule day")
                start, stop = schedule_days[0]["start"], schedule_days[0]["stop"]
                selected_days = [row["weekday"] for row in enabled_days]
            timezone_value = str(
                state["schedule_timezone"] if timezone_name is None else timezone_name
            ).strip()
            self._schedule_timezone(timezone_value)
            return self.state.record_activity(
                self.target,
                "schedule_enabled" if enabled else "schedule_disabled",
                "Power schedule enabled" if enabled else "Power schedule paused",
                schedule_enabled=enabled,
                schedule_start=start,
                schedule_stop=stop,
                schedule_weekdays=selected_days,
                schedule_timezone=timezone_value,
                schedule_days=schedule_days,
                last_schedule_event=None,
            )

    def next_schedule_event(
        self,
        state: Optional[Dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> Optional[Dict[str, str]]:
        state = state or self.state.target(self.target)
        if not state["schedule_enabled"]:
            return None
        zone = self._schedule_timezone(state["schedule_timezone"])
        local_now = self._local_schedule_time(now, zone)
        candidates = []
        for offset in range(-1, 8):
            start_date = (local_now + timedelta(days=offset)).date()
            row = self.schedule_days(state)[start_date.weekday()]
            if not row["enabled"]:
                continue
            start_time = datetime_time.fromisoformat(row["start"])
            stop_time = datetime_time.fromisoformat(row["stop"])
            overnight = start_time >= stop_time
            start_at = datetime.combine(start_date, start_time, tzinfo=zone)
            stop_date = start_date + timedelta(days=1) if overnight else start_date
            stop_at = datetime.combine(stop_date, stop_time, tzinfo=zone)
            if start_at > local_now:
                candidates.append((start_at, "start"))
            if stop_at > local_now:
                candidates.append((stop_at, "stop"))
        if not candidates:
            return None
        candidate, action = min(candidates, key=lambda item: item[0])
        return {"at": candidate.isoformat(timespec="minutes"), "action": action}

    @staticmethod
    def _local_schedule_time(now: Optional[datetime], zone: tzinfo) -> datetime:
        if now is None:
            return datetime.now(zone)
        if now.tzinfo is None:
            return now.replace(tzinfo=zone)
        return now.astimezone(zone)

    def schedule_window_active(
        self,
        state: Optional[Dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> bool:
        state = state or self.state.target(self.target)
        if not state["schedule_enabled"]:
            return True
        zone = self._schedule_timezone(state["schedule_timezone"])
        local_now = self._local_schedule_time(now, zone)
        current = local_now.timetz().replace(tzinfo=None)
        today = self.schedule_days(state)[local_now.weekday()]
        if today["enabled"]:
            start = datetime_time.fromisoformat(today["start"])
            stop = datetime_time.fromisoformat(today["stop"])
            if start < stop and start <= current < stop:
                return True
            if start >= stop and current >= start:
                return True
        previous = self.schedule_days(state)[(local_now - timedelta(days=1)).weekday()]
        if previous["enabled"]:
            previous_start = datetime_time.fromisoformat(previous["start"])
            previous_stop = datetime_time.fromisoformat(previous["stop"])
            if previous_start >= previous_stop and current < previous_stop:
                return True
        return False

    def check_schedule(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        state = self.state.target(self.target)
        if not state["schedule_enabled"]:
            self._record("schedule_check", "Power schedule is paused", "skipped")
            return {"target_id": self.target.id, "skipped": True, "message": "Power schedule is paused"}
        zone = self._schedule_timezone(state["schedule_timezone"])
        local_now = self._local_schedule_time(now, zone)
        current_time = local_now.strftime("%H:%M")
        events = []
        for offset in (0, -1):
            start_date = (local_now + timedelta(days=offset)).date()
            row = self.schedule_days(state)[start_date.weekday()]
            if not row["enabled"]:
                continue
            start_time = datetime_time.fromisoformat(row["start"])
            stop_time = datetime_time.fromisoformat(row["stop"])
            stop_date = start_date + timedelta(days=1) if start_time >= stop_time else start_date
            if offset == 0 and current_time == row["start"]:
                events.append(("start", row))
            if stop_date == local_now.date() and current_time == row["stop"]:
                events.append(("stop", row))
        if not events:
            self._record("schedule_check", "No schedule event is due", "skipped")
            return {"target_id": self.target.id, "skipped": True, "message": "No schedule event is due"}
        decision, row = events[0]
        event_key = f"{local_now.date().isoformat()}:{current_time}:{row['weekday']}:{decision}"
        with self.exclusive():
            locked_state = self.state.target(self.target)
            if locked_state.get("last_schedule_event") == event_key:
                self._record("schedule_check", "Schedule event was already handled", "skipped")
                return {"target_id": self.target.id, "skipped": True, "message": "Schedule event was already handled"}
            try:
                action = self._ensure_power_locked(decision)
                self.state.record_activity(
                    self.target, f"schedule_{decision}", action["message"], last_schedule_event=event_key
                )
            except Exception as exc:
                self._record(f"schedule_{decision}", str(exc), "failed")
                raise
        return {
            "target_id": self.target.id,
            "skipped": False,
            "decision": decision,
            "scheduled_time": current_time,
            "timezone": state["schedule_timezone"],
            **action,
        }

    @staticmethod
    def _activity_source(action: object) -> str:
        value = str(action or "")
        if value.startswith("traffic_"):
            return "traffic"
        if value.startswith("schedule_"):
            return "schedule"
        if value in {"start", "stop"}:
            return "manual"
        return "config"

    def activity_history(self, source: str = "all", outcome: str = "all", offset: int = 0, limit: int = 50) -> Dict[str, Any]:
        if source not in ACTIVITY_SOURCES:
            raise ValueError("activity source is invalid")
        if outcome not in ACTIVITY_OUTCOMES:
            raise ValueError("activity outcome is invalid")
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("activity limit is invalid")
        state = self.state.target(self.target)
        history = list(reversed(self.state._recent_history(state.get("activity_history", []))))
        if not isinstance(offset, int) or not 0 <= offset <= len(history):
            raise ValueError("activity offset is invalid")
        filtered = [
            item for item in history
            if (source == "all" or self._activity_source(item.get("action")) == source)
            and (outcome == "all" or item.get("outcome") == outcome)
        ]
        page = filtered[offset:offset + limit]
        return {
            "items": page,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(page) < len(filtered),
        }

    def check_policy(self, force: bool = False) -> Dict[str, Any]:
        state = self.state.target(self.target)
        if not state["auto_enabled"] and not force:
            self._record("traffic_check", "Automatic traffic policy is paused", "skipped")
            return {"target_id": self.target.id, "skipped": True, "message": "Automatic traffic policy is paused"}
        with self.exclusive():
            try:
                traffic = self.get_traffic_gb()
                decision = "start" if traffic < self.target.traffic_threshold_gb else "stop"
                action = self._ensure_power_locked(decision)
                self._record(f"traffic_{decision}", action["message"], last_traffic_gb=round(traffic, 2))
            except Exception as exc:
                self._record("traffic_check", str(exc), "failed")
                raise
        return {
            "target_id": self.target.id,
            "skipped": False,
            "decision": decision,
            "traffic_gb": round(traffic, 2),
            "threshold_gb": self.target.traffic_threshold_gb,
            **action,
        }

    def check_automation(self, now: Optional[datetime] = None, force_traffic: bool = False) -> Dict[str, Any]:
        state = self.state.target(self.target)
        schedule_enabled = bool(state["schedule_enabled"])
        traffic_enabled = bool(state["auto_enabled"] or force_traffic)
        if not schedule_enabled and not traffic_enabled:
            self._record("automation_check", "Automation is paused", "skipped")
            return {"target_id": self.target.id, "skipped": True, "message": "Automation is paused"}
        if schedule_enabled and not traffic_enabled:
            return self.check_schedule(now=now)
        if traffic_enabled and not schedule_enabled:
            return self.check_policy(force=force_traffic)

        in_window = self.schedule_window_active(state=state, now=now)
        with self.exclusive():
            traffic: Optional[float] = None
            if in_window:
                try:
                    traffic = self.get_traffic_gb()
                except Exception as exc:
                    self._record("traffic_check", f"Traffic query failed; power state unchanged: {exc}", "failed")
                    return {
                        "target_id": self.target.id,
                        "skipped": True,
                        "combined": True,
                        "in_schedule_window": True,
                        "message": "Traffic query failed; power state unchanged",
                        "error": str(exc),
                    }
                decision = "start" if traffic < self.target.traffic_threshold_gb else "stop"
                reason = "traffic_below_threshold" if decision == "start" else "traffic_threshold_reached"
            else:
                decision = "stop"
                reason = "outside_schedule_window"
            try:
                action = self._ensure_power_locked(decision)
                changes: Dict[str, Any] = {}
                if traffic is not None:
                    changes["last_traffic_gb"] = round(traffic, 2)
                action_name = f"traffic_{decision}" if in_window else f"schedule_{decision}"
                self._record(action_name, action["message"], **changes)
            except Exception as exc:
                self._record("traffic_check" if in_window else "schedule_check", str(exc), "failed")
                raise
        return {
            "target_id": self.target.id,
            "skipped": False,
            "combined": True,
            "in_schedule_window": in_window,
            "decision": decision,
            "reason": reason,
            "traffic_gb": round(traffic, 2) if traffic is not None else None,
            "threshold_gb": self.target.traffic_threshold_gb,
            **action,
        }


class ControlFleet:
    def __init__(self, config: Config, clients: Optional[Dict[str, Any]] = None) -> None:
        self.config = config
        self.state = StateStore(config.state_path)
        self.clients = clients or {}
        self._config_lock = threading.RLock()
        self._mutation_lock = threading.Lock()
        self.accounts = dict(config.accounts)
        self.targets = dict(config.targets)
        self.store: Optional[ManagedConfigStore] = None
        if config.managed_config_path and config.credential_key_path:
            self.store = ManagedConfigStore(
                config.managed_config_path,
                config.credential_key_path,
                config.audit_path,
            )
            if not config.managed_config_path.exists():
                self.store.save(self.accounts, self.targets)

    def _snapshot(self) -> tuple[Dict[str, AccountConfig], Dict[str, TargetConfig]]:
        with self._config_lock:
            return dict(self.accounts), dict(self.targets)

    @staticmethod
    def _masked_access_key(value: str) -> str:
        if len(value) <= 8:
            return "****"
        return f"{value[:4]}...{value[-4:]}"

    def configuration(self) -> Dict[str, Any]:
        accounts, targets = self._snapshot()
        return {
            "accounts": [
                {
                    "id": item.id,
                    "name": item.name,
                    "access_key_id_masked": self._masked_access_key(item.access_key_id),
                    "instance_count": sum(target.account_id == item.id for target in targets.values()),
                }
                for item in accounts.values()
            ],
            "instances": [
                {
                    "id": item.id,
                    "name": item.name,
                    "account": item.account_id,
                    "region": item.region_id,
                    "instance_id": item.instance_id,
                    "traffic_threshold_gb": item.traffic_threshold_gb,
                }
                for item in targets.values()
            ],
        }

    def _commit(
        self,
        accounts: Dict[str, AccountConfig],
        targets: Dict[str, TargetConfig],
        action: str,
        resource: str,
        actor: str = "",
    ) -> None:
        if self.store is None:
            raise RuntimeError("Managed configuration is not enabled")
        self.store.save(accounts, targets)
        with self._config_lock:
            self.accounts = accounts
            self.targets = targets
        try:
            self.store.audit(action, resource, actor)
        except OSError as exc:
            LOG.warning("Could not write configuration audit record: %s", exc)

    @staticmethod
    def _credential(value: object, field: str, minimum: int) -> str:
        text = _nonempty(value, field)
        if not minimum <= len(text) <= 256:
            raise ValueError(f"{field} length is invalid")
        return text

    def add_account(self, payload: Dict[str, Any], actor: str = "") -> Dict[str, Any]:
        with self._mutation_lock:
            return self._add_account(payload, actor)

    def _add_account(self, payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
        account_id = _identifier(payload.get("id"), "account id")
        accounts, targets = self._snapshot()
        if account_id in accounts:
            raise ValueError(f"Account already exists: {account_id}")
        account = AccountConfig(
            account_id,
            _nonempty(payload.get("name"), "account name"),
            self._credential(payload.get("access_key_id"), "AccessKey ID", 4),
            self._credential(payload.get("access_key_secret"), "AccessKey Secret", 8),
        )
        accounts[account_id] = account
        self._commit(accounts, targets, "account_created", account_id, actor)
        return self.configuration()["accounts"][-1]

    def update_account(self, account_id: str, payload: Dict[str, Any], actor: str = "") -> Dict[str, Any]:
        with self._mutation_lock:
            return self._update_account(account_id, payload, actor)

    def _update_account(self, account_id: str, payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
        accounts, targets = self._snapshot()
        current = accounts.get(account_id)
        if current is None:
            raise KeyError(f"Unknown account: {account_id}")
        access_key_id = payload.get("access_key_id")
        access_key_secret = payload.get("access_key_secret")
        account = AccountConfig(
            current.id,
            _nonempty(payload.get("name", current.name), "account name"),
            current.access_key_id if access_key_id is None or access_key_id == "" else self._credential(access_key_id, "AccessKey ID", 4),
            current.access_key_secret if access_key_secret is None or access_key_secret == "" else self._credential(access_key_secret, "AccessKey Secret", 8),
        )
        accounts[account_id] = account
        self._commit(accounts, targets, "account_updated", account_id, actor)
        return next(item for item in self.configuration()["accounts"] if item["id"] == account_id)

    def delete_account(self, account_id: str, actor: str = "") -> Dict[str, Any]:
        with self._mutation_lock:
            return self._delete_account(account_id, actor)

    def _delete_account(self, account_id: str, actor: str) -> Dict[str, Any]:
        accounts, targets = self._snapshot()
        if account_id not in accounts:
            raise KeyError(f"Unknown account: {account_id}")
        if any(target.account_id == account_id for target in targets.values()):
            raise ValueError("Delete or reassign instances that use this account first")
        accounts.pop(account_id)
        self._commit(accounts, targets, "account_deleted", account_id, actor)
        return {"id": account_id, "deleted": True}

    def test_credentials(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        account_id = str(payload.get("account_id") or "").strip()
        if account_id:
            accounts, _ = self._snapshot()
            current = accounts.get(account_id)
            if current is None:
                raise KeyError(f"Unknown account: {account_id}")
        else:
            current = None
        access_key_id = payload.get("access_key_id") or (current.access_key_id if current else None)
        access_key_secret = payload.get("access_key_secret") or (current.access_key_secret if current else None)
        key_id = self._credential(access_key_id, "AccessKey ID", 4)
        key_secret = self._credential(access_key_secret, "AccessKey Secret", 8)
        client = AcsClient(key_id, key_secret, "cn-hangzhou")
        request = CommonRequest()
        request.set_domain("sts.aliyuncs.com")
        request.set_version("2015-04-01")
        request.set_action_name("GetCallerIdentity")
        request.set_method("POST")
        try:
            self._call_with_client(client, request)
        except Exception as exc:
            LOG.warning("AccessKey validation failed: %s", type(exc).__name__)
            raise ValueError("AccessKey validation failed") from None
        return {"valid": True, "message": "AccessKey validation succeeded"}

    @staticmethod
    def _call_with_client(client: Any, request: Any) -> Dict[str, Any]:
        response = client.do_action_with_exception(request)
        return json.loads(response.decode("utf-8"))

    @staticmethod
    def _target_from_payload(payload: Dict[str, Any], current: Optional[TargetConfig] = None) -> TargetConfig:
        target_id = current.id if current else _identifier(payload.get("id"), "instance id")
        account_id = _identifier(payload.get("account", current.account_id if current else None), "instance account")
        try:
            threshold = float(payload.get("traffic_threshold_gb", current.traffic_threshold_gb if current else 180))
        except (TypeError, ValueError) as exc:
            raise ValueError("traffic threshold must be a number") from exc
        if not math.isfinite(threshold) or threshold <= 0:
            raise ValueError("traffic threshold must be greater than zero")
        return TargetConfig(
            id=target_id,
            name=_nonempty(payload.get("name", current.name if current else None), "instance name"),
            account_id=account_id,
            region_id=_nonempty(payload.get("region", current.region_id if current else None), "region"),
            instance_id=_nonempty(payload.get("instance_id", current.instance_id if current else None), "ECS instance id"),
            traffic_threshold_gb=threshold,
            auto_enabled_default=current.auto_enabled_default if current else False,
            schedule_enabled_default=current.schedule_enabled_default if current else False,
            schedule_start_default=current.schedule_start_default if current else "08:00",
            schedule_stop_default=current.schedule_stop_default if current else "23:00",
            schedule_weekdays_default=current.schedule_weekdays_default if current else (0, 1, 2, 3, 4, 5, 6),
            schedule_timezone_default=current.schedule_timezone_default if current else _timezone_value(payload.get("timezone", "Asia/Shanghai"), "timezone"),
        )

    def add_target(self, payload: Dict[str, Any], actor: str = "") -> Dict[str, Any]:
        with self._mutation_lock:
            return self._add_target(payload, actor)

    def _add_target(self, payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
        accounts, targets = self._snapshot()
        target = self._target_from_payload(payload)
        if target.id in targets:
            raise ValueError(f"Instance already exists: {target.id}")
        if target.account_id not in accounts:
            raise ValueError(f"Unknown account: {target.account_id}")
        targets[target.id] = target
        self._commit(accounts, targets, "instance_created", target.id, actor)
        return next(item for item in self.configuration()["instances"] if item["id"] == target.id)

    def update_target(self, target_id: str, payload: Dict[str, Any], actor: str = "") -> Dict[str, Any]:
        with self._mutation_lock:
            return self._update_target(target_id, payload, actor)

    def set_threshold(self, target_id: str, value: object, actor: str = "") -> Dict[str, Any]:
        with self._mutation_lock:
            accounts, targets = self._snapshot()
            current = targets.get(target_id)
            if current is None:
                raise KeyError(f"Unknown target: {target_id}")
            target = self._target_from_payload({"traffic_threshold_gb": value}, current=current)
            targets[target_id] = target
            self._commit(accounts, targets, "traffic_threshold_updated", target_id, actor)
            return {"target_id": target_id, "threshold_gb": target.traffic_threshold_gb}

    def _update_target(self, target_id: str, payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
        accounts, targets = self._snapshot()
        current = targets.get(target_id)
        if current is None:
            raise KeyError(f"Unknown target: {target_id}")
        target = self._target_from_payload(payload, current=current)
        if target.account_id not in accounts:
            raise ValueError(f"Unknown account: {target.account_id}")
        targets[target_id] = target
        self.clients.pop(target_id, None)
        self._commit(accounts, targets, "instance_updated", target_id, actor)
        return next(item for item in self.configuration()["instances"] if item["id"] == target_id)

    def delete_target(self, target_id: str, actor: str = "") -> Dict[str, Any]:
        with self._mutation_lock:
            return self._delete_target(target_id, actor)

    def _delete_target(self, target_id: str, actor: str) -> Dict[str, Any]:
        accounts, targets = self._snapshot()
        if target_id not in targets:
            raise KeyError(f"Unknown target: {target_id}")
        targets.pop(target_id)
        self.clients.pop(target_id, None)
        self._commit(accounts, targets, "instance_deleted", target_id, actor)
        self.state.remove_target(target_id)
        return {"id": target_id, "deleted": True}

    def controller(self, target_id: str) -> EcsController:
        accounts, targets = self._snapshot()
        target = targets.get(target_id)
        if target is None:
            raise KeyError(f"Unknown target: {target_id}")
        account = accounts[target.account_id]
        return EcsController(
            self.config,
            target,
            account,
            self.state,
            client=self.clients.get(target_id),
        )

    def overview(self) -> Dict[str, Any]:
        _, targets = self._snapshot()
        results: Dict[str, Dict[str, Any]] = {}
        if not targets:
            return {"targets": [], "target_count": 0}
        worker_count = min(6, max(1, len(targets)))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(self.controller(target_id).overview): target_id
                for target_id in targets
            }
            for future in as_completed(futures):
                target_id = futures[future]
                controller = self.controller(target_id)
                try:
                    results[target_id] = future.result()
                except Exception as exc:
                    LOG.warning("Overview failed for %s: %s", target_id, exc)
                    results[target_id] = {
                        **controller.metadata(),
                        "status": "Error",
                        "traffic_gb": None,
                        "usage_percent": 0,
                        "error": str(exc),
                        **self.state.target(controller.target),
                    }
        ordered = [results[target_id] for target_id in targets]
        return {"targets": ordered, "target_count": len(ordered)}

    def check_all(self) -> Dict[str, Any]:
        _, targets = self._snapshot()
        results = []
        check_time = datetime.now(timezone.utc)
        for target_id in targets:
            try:
                controller = self.controller(target_id)
                results.append({"target_id": target_id, "automation": controller.check_automation(now=check_time)})
            except Exception as exc:
                LOG.exception("Automation check failed for %s", target_id)
                controller.record_failure("automation_check", exc)
                results.append({"target_id": target_id, "error": str(exc)})
        return {"targets": results}
