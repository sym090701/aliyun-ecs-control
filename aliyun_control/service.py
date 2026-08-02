from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

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

from .config import AccountConfig, Config, TargetConfig

LOG = logging.getLogger(__name__)


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
        defaults = {
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
        }
        stored = self.load_all().get("targets", {}).get(target.id, {})
        if isinstance(stored, dict):
            defaults.update(stored)
        return defaults

    def update_target(self, target: TargetConfig, **changes: Any) -> Dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock:
            with self.lock_path.open("a", encoding="utf-8") as lock_handle:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                payload = self.load_all()
                current = self.target(target)
                current.update(changes)
                payload.setdefault("targets", {})[target.id] = current
                self._save(payload)
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                return current

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
        }

    def _record(self, action: str, result: str, **extra: Any) -> None:
        self.state.update_target(
            self.target,
            last_check=utc_now(),
            last_action=action,
            last_result=result,
            **extra,
        )

    def start(self) -> Dict[str, Any]:
        with self.exclusive():
            before = self.get_status()
            if before in {"Running", "Starting"}:
                result = {"changed": False, "status": before, "message": "Instance is already running"}
            else:
                request = StartInstancesRequest.StartInstancesRequest()
                request.set_InstanceIds([self.target.instance_id])
                request.set_accept_format("json")
                self._call(request)
                result = {"changed": True, "status": "Starting", "message": "Start request accepted"}
            self._record("start", result["message"])
            return {"target_id": self.target.id, **result}

    def stop(self) -> Dict[str, Any]:
        with self.exclusive():
            before = self.get_status()
            if before in {"Stopped", "Stopping"}:
                result = {"changed": False, "status": before, "message": "Instance is already stopped"}
            else:
                request = StopInstancesRequest.StopInstancesRequest()
                request.set_InstanceIds([self.target.instance_id])
                request.set_ForceStop(False)
                request.set_accept_format("json")
                self._call(request)
                result = {"changed": True, "status": "Stopping", "message": "Graceful stop request accepted"}
            self._record("stop", result["message"])
            return {"target_id": self.target.id, **result}

    def set_auto(self, enabled: bool) -> Dict[str, Any]:
        with self.exclusive():
            state = self.state.target(self.target)
            if enabled and state["schedule_enabled"]:
                raise ValueError("Disable the power schedule before enabling traffic policy")
            return self.state.update_target(
                self.target,
                auto_enabled=enabled,
                last_check=utc_now(),
                last_action="auto_enabled" if enabled else "auto_disabled",
                last_result="Automatic traffic policy enabled" if enabled else "Automatic traffic policy paused",
            )

    @staticmethod
    def _schedule_timezone(value: object) -> ZoneInfo:
        name = str(value or "").strip()
        if not name:
            raise ValueError("schedule timezone cannot be empty")
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

    def set_schedule(
        self,
        enabled: bool,
        start_time: Optional[object] = None,
        stop_time: Optional[object] = None,
        weekdays: Optional[object] = None,
        timezone_name: Optional[object] = None,
    ) -> Dict[str, Any]:
        if not isinstance(enabled, bool):
            raise ValueError("schedule enabled must be a boolean")
        with self.exclusive():
            state = self.state.target(self.target)
            start = self._schedule_time(
                state["schedule_start"] if start_time is None else start_time,
                "schedule start",
            )
            stop = self._schedule_time(
                state["schedule_stop"] if stop_time is None else stop_time,
                "schedule stop",
            )
            if start == stop:
                raise ValueError("schedule start and stop times must differ")
            selected_days = self._schedule_weekdays(
                state["schedule_weekdays"] if weekdays is None else weekdays
            )
            timezone_value = str(
                state["schedule_timezone"] if timezone_name is None else timezone_name
            ).strip()
            self._schedule_timezone(timezone_value)
            if enabled and state["auto_enabled"]:
                raise ValueError("Disable traffic policy before enabling the power schedule")
            return self.state.update_target(
                self.target,
                schedule_enabled=enabled,
                schedule_start=start,
                schedule_stop=stop,
                schedule_weekdays=selected_days,
                schedule_timezone=timezone_value,
                last_schedule_event=None,
                last_check=utc_now(),
                last_action="schedule_enabled" if enabled else "schedule_disabled",
                last_result="Power schedule enabled" if enabled else "Power schedule paused",
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
        weekdays = set(self._schedule_weekdays(state["schedule_weekdays"]))
        candidates = []
        for offset in range(8):
            date = (local_now + timedelta(days=offset)).date()
            if date.weekday() not in weekdays:
                continue
            for action, value in (("start", state["schedule_start"]), ("stop", state["schedule_stop"])):
                parsed_time = datetime_time.fromisoformat(self._schedule_time(value, f"schedule {action}"))
                candidate = datetime.combine(date, parsed_time, tzinfo=zone)
                if candidate > local_now:
                    candidates.append((candidate, action))
        if not candidates:
            return None
        candidate, action = min(candidates, key=lambda item: item[0])
        return {"at": candidate.isoformat(timespec="minutes"), "action": action}

    @staticmethod
    def _local_schedule_time(now: Optional[datetime], zone: ZoneInfo) -> datetime:
        if now is None:
            return datetime.now(zone)
        if now.tzinfo is None:
            return now.replace(tzinfo=zone)
        return now.astimezone(zone)

    def check_schedule(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        state = self.state.target(self.target)
        if not state["schedule_enabled"]:
            return {"target_id": self.target.id, "skipped": True, "message": "Power schedule is paused"}
        zone = self._schedule_timezone(state["schedule_timezone"])
        local_now = self._local_schedule_time(now, zone)
        if local_now.weekday() not in self._schedule_weekdays(state["schedule_weekdays"]):
            return {"target_id": self.target.id, "skipped": True, "message": "Today is not scheduled"}
        current_time = local_now.strftime("%H:%M")
        if current_time == state["schedule_start"]:
            decision = "start"
        elif current_time == state["schedule_stop"]:
            decision = "stop"
        else:
            return {"target_id": self.target.id, "skipped": True, "message": "No schedule event is due"}
        event_key = f"{local_now.date().isoformat()}:{current_time}:{decision}"
        if state.get("last_schedule_event") == event_key:
            return {"target_id": self.target.id, "skipped": True, "message": "Schedule event was already handled"}
        action = self.start() if decision == "start" else self.stop()
        self.state.update_target(
            self.target,
            last_schedule_event=event_key,
            last_check=utc_now(),
            last_action=f"schedule_{decision}",
            last_result=action["message"],
        )
        return {
            "target_id": self.target.id,
            "skipped": False,
            "decision": decision,
            "scheduled_time": current_time,
            "timezone": state["schedule_timezone"],
            **action,
        }

    def check_policy(self, force: bool = False) -> Dict[str, Any]:
        state = self.state.target(self.target)
        if state["schedule_enabled"] and not force:
            return {"target_id": self.target.id, "skipped": True, "message": "Power schedule controls this instance"}
        if not state["auto_enabled"] and not force:
            return {"target_id": self.target.id, "skipped": True, "message": "Automatic traffic policy is paused"}
        traffic = self.get_traffic_gb()
        if traffic < self.target.traffic_threshold_gb:
            action = self.start()
            decision = "start"
        else:
            action = self.stop()
            decision = "stop"
        self._record(decision, action["message"], last_traffic_gb=round(traffic, 2))
        return {
            "target_id": self.target.id,
            "skipped": False,
            "decision": decision,
            "traffic_gb": round(traffic, 2),
            "threshold_gb": self.target.traffic_threshold_gb,
            **action,
        }


class ControlFleet:
    def __init__(self, config: Config, clients: Optional[Dict[str, Any]] = None) -> None:
        self.config = config
        self.state = StateStore(config.state_path)
        self.clients = clients or {}

    def controller(self, target_id: str) -> EcsController:
        target = self.config.targets.get(target_id)
        if target is None:
            raise KeyError(f"Unknown target: {target_id}")
        account = self.config.accounts[target.account_id]
        return EcsController(
            self.config,
            target,
            account,
            self.state,
            client=self.clients.get(target_id),
        )

    def overview(self) -> Dict[str, Any]:
        results: Dict[str, Dict[str, Any]] = {}
        worker_count = min(6, max(1, len(self.config.targets)))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(self.controller(target_id).overview): target_id
                for target_id in self.config.targets
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
        ordered = [results[target_id] for target_id in self.config.targets]
        return {"targets": ordered, "target_count": len(ordered)}

    def check_all(self) -> Dict[str, Any]:
        results = []
        check_time = datetime.now(timezone.utc)
        for target_id in self.config.targets:
            try:
                controller = self.controller(target_id)
                results.append(
                    {
                        "target_id": target_id,
                        "schedule": controller.check_schedule(now=check_time),
                        "traffic_policy": controller.check_policy(),
                    }
                )
            except Exception as exc:
                LOG.exception("Automation check failed for %s", target_id)
                results.append({"target_id": target_id, "error": str(exc)})
        return {"targets": results}
