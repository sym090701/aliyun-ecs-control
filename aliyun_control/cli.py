from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List

import pyotp

from .config import Config, generate_control_token, load_targets_file, write_targets_file
from .server import serve
from .service import ControlFleet


def output(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def positive_count(prompt: str) -> int:
    while True:
        try:
            value = int(input(prompt).strip())
            if value > 0:
                return value
        except ValueError:
            pass
        print("Enter a positive integer.")


def interactive_config(path: Path) -> None:
    accounts: List[Dict[str, Any]] = []
    instances: List[Dict[str, Any]] = []
    print("Configure Alibaba Cloud accounts")
    for index in range(positive_count("Number of accounts: ")):
        print(f"\nAccount {index + 1}")
        accounts.append(
            {
                "id": input("Short ID (for example cn or intl): ").strip(),
                "name": input("Display name: ").strip(),
                "access_key_id": input("AccessKey ID: ").strip(),
                "access_key_secret": getpass.getpass("AccessKey Secret: ").strip(),
            }
        )
    account_ids = ", ".join(item["id"] for item in accounts)
    print("\nConfigure ECS instances")
    for index in range(positive_count("Number of instances: ")):
        print(f"\nInstance {index + 1} (accounts: {account_ids})")
        threshold = input("Traffic threshold in GB [180]: ").strip() or "180"
        instances.append(
            {
                "id": input("Short ID: ").strip(),
                "name": input("Display name: ").strip(),
                "account": input("Account ID: ").strip(),
                "region": input("Region ID: ").strip(),
                "instance_id": input("ECS Instance ID: ").strip(),
                "traffic_threshold_gb": float(threshold),
                "auto_enabled_default": False,
                "schedule": {
                    "enabled": False,
                    "start": "08:00",
                    "stop": "23:00",
                    "weekdays": [0, 1, 2, 3, 4, 5, 6],
                    "timezone": "Asia/Shanghai",
                },
            }
        )
    write_targets_file(path, accounts, instances)
    load_targets_file(path)
    print(f"Configuration written to {path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aliyun-ecs-control")
    parser.add_argument("--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("overview", help="Show all configured ECS instances")
    for name in ("status", "traffic", "start", "stop"):
        command = commands.add_parser(name)
        command.add_argument("--target", required=True)
    check = commands.add_parser("check", help="Apply power schedules and automatic traffic policy")
    check.add_argument("--target", help="Check one target; omitted checks all targets")
    check.add_argument("--force", action="store_true")
    auto = commands.add_parser("auto", help="Enable or pause automatic policy")
    auto.add_argument("--target", required=True)
    auto.add_argument("state", choices=["on", "off"])
    schedule = commands.add_parser("schedule", help="Show or configure a target power schedule")
    schedule.add_argument("--target", required=True)
    schedule.add_argument("state", choices=["show", "on", "off"])
    schedule.add_argument("--start", help="Daily start time in HH:MM")
    schedule.add_argument("--stop", help="Daily stop time in HH:MM")
    schedule.add_argument("--weekdays", help="Comma-separated weekdays, Monday=0 through Sunday=6")
    schedule.add_argument("--timezone", help="IANA timezone, for example Asia/Shanghai")
    commands.add_parser("serve")
    commands.add_parser("generate-token")
    totp = commands.add_parser("generate-totp")
    totp.add_argument("--issuer", default="Aliyun ECS Control")
    totp.add_argument("--account", default="admin")
    initialize = commands.add_parser("init-config")
    initialize.add_argument("--path", type=Path, default=Path("/etc/aliyun-ecs-control-targets.json"))
    commands.add_parser("migrate-config", help="Encrypt the legacy bootstrap configuration")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.command == "generate-token":
        print(generate_control_token())
        return 0
    if args.command == "generate-totp":
        secret = pyotp.random_base32()
        print(f"TOTP_SECRET={secret}")
        print(pyotp.TOTP(secret).provisioning_uri(name=args.account, issuer_name=args.issuer))
        return 0
    if args.command == "init-config":
        interactive_config(args.path)
        return 0
    if args.command == "migrate-config":
        config = Config.from_env(require_web_token=False)
        ControlFleet(config)
        print(f"Managed configuration ready: {config.managed_config_path}")
        return 0

    config = Config.from_env(require_web_token=args.command == "serve")
    fleet = ControlFleet(config)
    if args.command == "serve":
        serve(config, fleet)
    elif args.command == "overview":
        output(fleet.overview())
    elif args.command == "check" and not args.target:
        output(fleet.check_all())
    else:
        controller = fleet.controller(args.target)
        if args.command == "status":
            output({"target_id": args.target, "status": controller.get_status()})
        elif args.command == "traffic":
            output({"target_id": args.target, "traffic_gb": round(controller.get_traffic_gb(), 2)})
        elif args.command == "start":
            output(controller.start())
        elif args.command == "stop":
            output(controller.stop())
        elif args.command == "check":
            if args.force:
                output(
                    {
                        "schedule": controller.check_schedule(),
                        "traffic_policy": controller.check_policy(force=True),
                    }
                )
            else:
                output(
                    {
                        "schedule": controller.check_schedule(),
                        "traffic_policy": controller.check_policy(),
                    }
                )
        elif args.command == "auto":
            output(controller.set_auto(args.state == "on"))
        elif args.command == "schedule":
            if args.state == "show":
                state = fleet.state.target(controller.target)
                output(
                    {
                        "target_id": args.target,
                        "enabled": state["schedule_enabled"],
                        "start": state["schedule_start"],
                        "stop": state["schedule_stop"],
                        "weekdays": state["schedule_weekdays"],
                        "timezone": state["schedule_timezone"],
                        "next": controller.next_schedule_event(state=state),
                    }
                )
            else:
                days = None
                if args.weekdays is not None:
                    try:
                        days = [int(value.strip()) for value in args.weekdays.split(",") if value.strip()]
                    except ValueError as exc:
                        raise ValueError("--weekdays must contain comma-separated integers") from exc
                output(
                    controller.set_schedule(
                        enabled=args.state == "on",
                        start_time=args.start,
                        stop_time=args.stop,
                        weekdays=days,
                        timezone_name=args.timezone,
                    )
                )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
