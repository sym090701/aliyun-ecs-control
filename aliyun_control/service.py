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

from .config import (
    AccountConfig,
    Config,
    ManagedConfigStore,
    TargetConfig,
    _identifier,
    _nonempty,
    _timezone_value,
)

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
        }

    def _record(self, action: str, result: str, **extra: Any) -> None:
        self.state.update_target(
            self.target,
            last_check=utc_now(),
            last_action=action,
            last_result=result,
            **extra,
        )

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
            result = self._ensure_power_locked("start")
            self._record("start", result["message"])
            return {"target_id": self.target.id, **result}

    def stop(self) -> Dict[str, Any]:
        with self.exclusive():
            result = self._ensure_power_locked("stop")
            self._record("stop", result["message"])
            return {"target_id": self.target.id, **result}

    def set_auto(self, enabled: bool) -> Dict[str, Any]:
        with self.exclusive():
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
        start_time = datetime_time.fromisoformat(self._schedule_time(state["schedule_start"], "schedule start"))
        stop_time = datetime_time.fromisoformat(self._schedule_time(state["schedule_stop"], "schedule stop"))
        overnight = start_time >= stop_time
        candidates = []
        for offset in range(-1, 8):
            start_date = (local_now + timedelta(days=offset)).date()
            if start_date.weekday() not in weekdays:
                continue
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
    def _local_schedule_time(now: Optional[datetime], zone: ZoneInfo) -> datetime:
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
        weekdays = set(self._schedule_weekdays(state["schedule_weekdays"]))
        start = datetime_time.fromisoformat(self._schedule_time(state["schedule_start"], "schedule start"))
        stop = datetime_time.fromisoformat(self._schedule_time(state["schedule_stop"], "schedule stop"))
        current = local_now.timetz().replace(tzinfo=None)
        if start < stop:
            return local_now.weekday() in weekdays and start <= current < stop
        if current >= start:
            return local_now.weekday() in weekdays
        previous_day = (local_now - timedelta(days=1)).weekday()
        return current < stop and previous_day in weekdays

    def check_schedule(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        state = self.state.target(self.target)
        if not state["schedule_enabled"]:
            return {"target_id": self.target.id, "skipped": True, "message": "Power schedule is paused"}
        zone = self._schedule_timezone(state["schedule_timezone"])
        local_now = self._local_schedule_time(now, zone)
        current_time = local_now.strftime("%H:%M")
        start_time = state["schedule_start"]
        stop_time = state["schedule_stop"]
        overnight = start_time >= stop_time
        weekdays = set(self._schedule_weekdays(state["schedule_weekdays"]))
        if current_time == state["schedule_start"]:
            if local_now.weekday() not in weekdays:
                return {"target_id": self.target.id, "skipped": True, "message": "Today is not scheduled"}
            decision = "start"
        elif current_time == state["schedule_stop"]:
            schedule_day = (local_now - timedelta(days=1)).weekday() if overnight else local_now.weekday()
            if schedule_day not in weekdays:
                return {"target_id": self.target.id, "skipped": True, "message": "Today is not scheduled"}
            decision = "stop"
        else:
            return {"target_id": self.target.id, "skipped": True, "message": "No schedule event is due"}
        event_key = f"{local_now.date().isoformat()}:{current_time}:{decision}"
        with self.exclusive():
            locked_state = self.state.target(self.target)
            if locked_state.get("last_schedule_event") == event_key:
                return {"target_id": self.target.id, "skipped": True, "message": "Schedule event was already handled"}
            action = self._ensure_power_locked(decision)
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
        if not state["auto_enabled"] and not force:
            return {"target_id": self.target.id, "skipped": True, "message": "Automatic traffic policy is paused"}
        with self.exclusive():
            traffic = self.get_traffic_gb()
            decision = "start" if traffic < self.target.traffic_threshold_gb else "stop"
            action = self._ensure_power_locked(decision)
            self._record(decision, action["message"], last_traffic_gb=round(traffic, 2))
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
                    self.state.update_target(
                        self.target,
                        last_check=utc_now(),
                        last_result=f"Traffic query failed; power state unchanged: {exc}",
                    )
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
            action = self._ensure_power_locked(decision)
            changes: Dict[str, Any] = {
                "last_check": utc_now(),
                "last_result": action["message"],
            }
            if traffic is not None:
                changes["last_traffic_gb"] = round(traffic, 2)
            if action["changed"]:
                changes["last_action"] = f"combined_{decision}"
            self.state.update_target(self.target, **changes)
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
                results.append({"target_id": target_id, "error": str(exc)})
        return {"targets": results}
