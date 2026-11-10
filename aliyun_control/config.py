from __future__ import annotations

import json
import math
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List, Optional, Tuple

from cryptography.fernet import Fernet, InvalidToken

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - exercised on Python 3.8 deployments
    from backports.zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
TIME_PATTERN = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
UTC_OFFSET_PATTERN = re.compile(r"^UTC([+-])(0[0-9]|1[0-4]):([0-5][0-9])$")


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _identifier(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not ID_PATTERN.fullmatch(text):
        raise ValueError(f"{field} must match {ID_PATTERN.pattern}")
    return text


def _nonempty(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} cannot be empty")
    return text


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _time_value(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not TIME_PATTERN.fullmatch(text):
        raise ValueError(f"{field} must use 24-hour HH:MM format")
    return text


def _weekdays(value: object, field: str) -> Tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty array")
    if any(isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6 for day in value):
        raise ValueError(f"{field} entries must be integers between 0 and 6")
    if len(set(value)) != len(value):
        raise ValueError(f"{field} cannot contain duplicates")
    return tuple(sorted(value))


def _timezone_value(value: object, field: str) -> str:
    name = _nonempty(value, field)
    match = UTC_OFFSET_PATTERN.fullmatch(name)
    if match:
        hours, minutes = int(match.group(2)), int(match.group(3))
        if hours == 14 and minutes:
            raise ValueError(f"{field} UTC offset must not exceed 14 hours")
        return name
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"{field} contains an unknown IANA timezone: {name}") from exc
    return name


@dataclass(frozen=True)
class AccountConfig:
    id: str
    name: str
    access_key_id: str
    access_key_secret: str


@dataclass(frozen=True)
class TargetConfig:
    id: str
    name: str
    account_id: str
    region_id: str
    instance_id: str
    traffic_threshold_gb: float
    auto_enabled_default: bool = False
    schedule_enabled_default: bool = False
    schedule_start_default: str = "08:00"
    schedule_stop_default: str = "23:00"
    schedule_weekdays_default: Tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)
    schedule_timezone_default: str = "Asia/Shanghai"

    @property
    def instance_label(self) -> str:
        if len(self.instance_id) <= 12:
            return self.instance_id
        return f"{self.instance_id[:6]}...{self.instance_id[-4:]}"


@dataclass(frozen=True)
class Config:
    accounts: Dict[str, AccountConfig]
    targets: Dict[str, TargetConfig]
    control_token: str
    totp_secret: str
    listen_host: str
    listen_port: int
    state_path: Path
    lock_dir: Path
    cookie_secure: bool
    trust_proxy: bool
    session_hours: int
    managed_config_path: Optional[Path] = None
    credential_key_path: Optional[Path] = None
    audit_path: Optional[Path] = None
    passkey_path: Optional[Path] = None
    passkey_rp_id: str = "ali.hkz.241214.top"
    passkey_origin: str = "https://ali.hkz.241214.top"

    @classmethod
    def from_env(cls, require_web_token: bool = True) -> "Config":
        token = os.getenv("CONTROL_TOKEN", "").strip()
        if require_web_token and len(token) < 24:
            raise ValueError("CONTROL_TOKEN must contain at least 24 characters")

        targets_file = Path(os.getenv("TARGETS_FILE", "/etc/aliyun-ecs-control-targets.json"))
        managed_config_path = Path(
            os.getenv("MANAGED_CONFIG_PATH", "/var/lib/aliyun-ecs-control/config.json")
        )
        credential_key_path = Path(
            os.getenv("CREDENTIAL_KEY_PATH", "/etc/aliyun-ecs-control.key")
        )
        if managed_config_path.exists():
            accounts, targets = load_managed_config(managed_config_path, credential_key_path)
        else:
            accounts, targets = load_targets_file(targets_file)
        port = int(os.getenv("LISTEN_PORT", "8787"))
        if not 1 <= port <= 65535:
            raise ValueError("LISTEN_PORT must be between 1 and 65535")
        session_hours = int(os.getenv("SESSION_HOURS", "8"))
        if not 1 <= session_hours <= 72:
            raise ValueError("SESSION_HOURS must be between 1 and 72")

        return cls(
            accounts=accounts,
            targets=targets,
            control_token=token,
            totp_secret=os.getenv("TOTP_SECRET", "").replace(" ", "").strip().upper(),
            listen_host=os.getenv("LISTEN_HOST", "127.0.0.1").strip(),
            listen_port=port,
            state_path=Path(os.getenv("STATE_PATH", "/var/lib/aliyun-ecs-control/state.json")),
            lock_dir=Path(os.getenv("LOCK_DIR", "/var/lib/aliyun-ecs-control/locks")),
            cookie_secure=_bool_env("COOKIE_SECURE", False),
            trust_proxy=_bool_env("TRUST_PROXY", False),
            session_hours=session_hours,
            managed_config_path=managed_config_path,
            credential_key_path=credential_key_path,
            audit_path=Path(os.getenv("AUDIT_PATH", "/var/lib/aliyun-ecs-control/audit.log")),
            passkey_path=Path(os.getenv("PASSKEY_PATH", "/var/lib/aliyun-ecs-control/passkeys.json")),
            passkey_rp_id=os.getenv("PASSKEY_RP_ID", "ali.hkz.241214.top").strip(),
            passkey_origin=os.getenv("PASSKEY_ORIGIN", "https://ali.hkz.241214.top").strip(),
        )


def load_targets_file(path: Path) -> tuple[Dict[str, AccountConfig], Dict[str, TargetConfig]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError as exc:
        raise ValueError(f"Targets file not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read targets file {path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("Targets file root must be a JSON object")
    raw_accounts = payload.get("accounts")
    raw_targets = payload.get("instances")
    if not isinstance(raw_accounts, list) or not raw_accounts:
        raise ValueError("Targets file must contain a non-empty accounts array")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ValueError("Targets file must contain a non-empty instances array")

    accounts: Dict[str, AccountConfig] = {}
    for index, item in enumerate(raw_accounts):
        if not isinstance(item, dict):
            raise ValueError(f"accounts[{index}] must be an object")
        account_id = _identifier(item.get("id"), f"accounts[{index}].id")
        if account_id in accounts:
            raise ValueError(f"Duplicate account id: {account_id}")
        accounts[account_id] = AccountConfig(
            id=account_id,
            name=_nonempty(item.get("name"), f"accounts[{index}].name"),
            access_key_id=_nonempty(item.get("access_key_id"), f"accounts[{index}].access_key_id"),
            access_key_secret=_nonempty(item.get("access_key_secret"), f"accounts[{index}].access_key_secret"),
        )

    targets: Dict[str, TargetConfig] = {}
    for index, item in enumerate(raw_targets):
        if not isinstance(item, dict):
            raise ValueError(f"instances[{index}] must be an object")
        target_id = _identifier(item.get("id"), f"instances[{index}].id")
        if target_id in targets:
            raise ValueError(f"Duplicate instance id: {target_id}")
        account_id = _identifier(item.get("account"), f"instances[{index}].account")
        if account_id not in accounts:
            raise ValueError(f"Instance {target_id} references unknown account {account_id}")
        threshold = float(item.get("traffic_threshold_gb", 180))
        if not math.isfinite(threshold) or threshold <= 0:
            raise ValueError(f"Instance {target_id} traffic_threshold_gb must be greater than zero")
        auto_enabled_default = item.get("auto_enabled_default", False)
        raw_schedule = item.get("schedule", {})
        if not isinstance(raw_schedule, dict):
            raise ValueError(f"instances[{index}].schedule must be an object")
        schedule_enabled = _boolean(
            raw_schedule.get("enabled", False),
            f"instances[{index}].schedule.enabled",
        )
        schedule_start = _time_value(
            raw_schedule.get("start", "08:00"),
            f"instances[{index}].schedule.start",
        )
        schedule_stop = _time_value(
            raw_schedule.get("stop", "23:00"),
            f"instances[{index}].schedule.stop",
        )
        if schedule_start == schedule_stop:
            raise ValueError(f"Instance {target_id} schedule start and stop times must differ")
        schedule_weekdays = _weekdays(
            raw_schedule.get("weekdays", [0, 1, 2, 3, 4, 5, 6]),
            f"instances[{index}].schedule.weekdays",
        )
        schedule_timezone = _timezone_value(
            raw_schedule.get("timezone", "Asia/Shanghai"),
            f"instances[{index}].schedule.timezone",
        )
        targets[target_id] = TargetConfig(
            id=target_id,
            name=_nonempty(item.get("name"), f"instances[{index}].name"),
            account_id=account_id,
            region_id=_nonempty(item.get("region"), f"instances[{index}].region"),
            instance_id=_nonempty(item.get("instance_id"), f"instances[{index}].instance_id"),
            traffic_threshold_gb=threshold,
            auto_enabled_default=_boolean(
                auto_enabled_default,
                f"instances[{index}].auto_enabled_default",
            ),
            schedule_enabled_default=schedule_enabled,
            schedule_start_default=schedule_start,
            schedule_stop_default=schedule_stop,
            schedule_weekdays_default=schedule_weekdays,
            schedule_timezone_default=schedule_timezone,
        )
    return accounts, targets


def generate_control_token() -> str:
    return secrets.token_urlsafe(32)


def write_targets_file(path: Path, accounts: List[dict], instances: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"accounts": accounts, "instances": instances}
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _parse_payload(payload: object, *, allow_empty: bool = False) -> tuple[Dict[str, AccountConfig], Dict[str, TargetConfig]]:
    if not isinstance(payload, dict):
        raise ValueError("Targets file root must be a JSON object")
    raw_accounts = payload.get("accounts", [])
    raw_targets = payload.get("instances", [])
    if not isinstance(raw_accounts, list) or (not raw_accounts and not allow_empty):
        raise ValueError("Targets file must contain a non-empty accounts array")
    if not isinstance(raw_targets, list) or (not raw_targets and not allow_empty):
        raise ValueError("Targets file must contain a non-empty instances array")

    accounts: Dict[str, AccountConfig] = {}
    for index, item in enumerate(raw_accounts):
        if not isinstance(item, dict):
            raise ValueError(f"accounts[{index}] must be an object")
        account_id = _identifier(item.get("id"), f"accounts[{index}].id")
        if account_id in accounts:
            raise ValueError(f"Duplicate account id: {account_id}")
        access_key_id = _nonempty(item.get("access_key_id"), f"accounts[{index}].access_key_id")
        access_key_secret = _nonempty(item.get("access_key_secret"), f"accounts[{index}].access_key_secret")
        if not 1 <= len(access_key_id) <= 128:
            raise ValueError(f"accounts[{index}].access_key_id length is invalid")
        if not 1 <= len(access_key_secret) <= 256:
            raise ValueError(f"accounts[{index}].access_key_secret length is invalid")
        accounts[account_id] = AccountConfig(
            id=account_id,
            name=_nonempty(item.get("name"), f"accounts[{index}].name"),
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
        )

    targets: Dict[str, TargetConfig] = {}
    for index, item in enumerate(raw_targets):
        if not isinstance(item, dict):
            raise ValueError(f"instances[{index}] must be an object")
        target_id = _identifier(item.get("id"), f"instances[{index}].id")
        if target_id in targets:
            raise ValueError(f"Duplicate instance id: {target_id}")
        account_id = _identifier(item.get("account"), f"instances[{index}].account")
        if account_id not in accounts:
            raise ValueError(f"Instance {target_id} references unknown account {account_id}")
        threshold = float(item.get("traffic_threshold_gb", 180))
        if not math.isfinite(threshold) or threshold <= 0:
            raise ValueError(f"Instance {target_id} traffic_threshold_gb must be greater than zero")
        auto_enabled_default = _boolean(item.get("auto_enabled_default", False), f"instances[{index}].auto_enabled_default")
        raw_schedule = item.get("schedule", {})
        if not isinstance(raw_schedule, dict):
            raise ValueError(f"instances[{index}].schedule must be an object")
        schedule_enabled = _boolean(raw_schedule.get("enabled", False), f"instances[{index}].schedule.enabled")
        schedule_start = _time_value(raw_schedule.get("start", "08:00"), f"instances[{index}].schedule.start")
        schedule_stop = _time_value(raw_schedule.get("stop", "23:00"), f"instances[{index}].schedule.stop")
        if schedule_start == schedule_stop:
            raise ValueError(f"Instance {target_id} schedule start and stop times must differ")
        schedule_weekdays = _weekdays(raw_schedule.get("weekdays", [0, 1, 2, 3, 4, 5, 6]), f"instances[{index}].schedule.weekdays")
        schedule_timezone = _timezone_value(raw_schedule.get("timezone", "Asia/Shanghai"), f"instances[{index}].schedule.timezone")
        targets[target_id] = TargetConfig(
            id=target_id,
            name=_nonempty(item.get("name"), f"instances[{index}].name"),
            account_id=account_id,
            region_id=_nonempty(item.get("region"), f"instances[{index}].region"),
            instance_id=_nonempty(item.get("instance_id"), f"instances[{index}].instance_id"),
            traffic_threshold_gb=threshold,
            auto_enabled_default=auto_enabled_default,
            schedule_enabled_default=schedule_enabled,
            schedule_start_default=schedule_start,
            schedule_stop_default=schedule_stop,
            schedule_weekdays_default=schedule_weekdays,
            schedule_timezone_default=schedule_timezone,
        )
    return accounts, targets


def _account_payload(account: AccountConfig, cipher: Fernet) -> Dict[str, str]:
    return {
        "id": account.id,
        "name": account.name,
        "access_key_id": cipher.encrypt(account.access_key_id.encode("utf-8")).decode("ascii"),
        "access_key_secret": cipher.encrypt(account.access_key_secret.encode("utf-8")).decode("ascii"),
    }


def _target_payload(target: TargetConfig) -> Dict[str, Any]:
    return {
        "id": target.id,
        "name": target.name,
        "account": target.account_id,
        "region": target.region_id,
        "instance_id": target.instance_id,
        "traffic_threshold_gb": target.traffic_threshold_gb,
        "auto_enabled_default": target.auto_enabled_default,
        "schedule": {
            "enabled": target.schedule_enabled_default,
            "start": target.schedule_start_default,
            "stop": target.schedule_stop_default,
            "weekdays": list(target.schedule_weekdays_default),
            "timezone": target.schedule_timezone_default,
        },
    }


def _load_fernet(path: Path) -> Fernet:
    try:
        key = path.read_bytes().strip()
        return Fernet(key)
    except (OSError, ValueError) as exc:
        raise ValueError(f"Could not read credential key {path}: {exc}") from exc


def load_managed_config(path: Path, key_path: Path) -> tuple[Dict[str, AccountConfig], Dict[str, TargetConfig]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("managed configuration root must be an object")
        cipher = _load_fernet(key_path)
        decoded_accounts = []
        for item in payload.get("accounts", []):
            item = dict(item)
            for field in ("access_key_id", "access_key_secret"):
                item[field] = cipher.decrypt(str(item[field]).encode("ascii")).decode("utf-8")
            decoded_accounts.append(item)
        return _parse_payload({"accounts": decoded_accounts, "instances": payload.get("instances", [])}, allow_empty=True)
    except (OSError, json.JSONDecodeError, KeyError, InvalidToken, UnicodeDecodeError, TypeError, ValueError) as exc:
        raise ValueError(f"Could not read managed configuration {path}: {exc}") from exc


class ManagedConfigStore:
    """Atomically stores the editable account and instance configuration."""

    def __init__(self, path: Path, key_path: Path, audit_path: Optional[Path] = None) -> None:
        self.path = path
        self.key_path = key_path
        self.audit_path = audit_path
        self._lock = RLock()

    def save(self, accounts: Dict[str, AccountConfig], targets: Dict[str, TargetConfig]) -> None:
        with self._lock:
            cipher = _load_fernet(self.key_path)
            payload = {
                "version": 2,
                "accounts": [_account_payload(item, cipher) for item in accounts.values()],
                "instances": [_target_payload(item) for item in targets.values()],
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(6)}.tmp")
            try:
                with temporary.open("x", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=True, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, self.path)
            finally:
                if temporary.exists():
                    temporary.unlink()

    def audit(self, action: str, resource: str, actor: str = "") -> None:
        if not self.audit_path:
            return
        with self._lock:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {"at": utc_audit_now(), "actor": actor, "action": action, "resource": resource}
                    )
                    + "\n"
                )
            os.chmod(self.audit_path, 0o600)


def utc_audit_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")
