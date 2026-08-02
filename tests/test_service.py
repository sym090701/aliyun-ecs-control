import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from aliyun_control.config import AccountConfig, Config, TargetConfig
from aliyun_control.service import ControlFleet


class FakeClient:
    def __init__(self, status="Stopped", traffic_gb=20, traffic_error=None):
        self.status = status
        self.traffic_gb = traffic_gb
        self.traffic_error = traffic_error
        self.calls = []

    def do_action_with_exception(self, request):
        name = request.get_action_name()
        self.calls.append(name)
        if name == "DescribeInstances":
            payload = {"Instances": {"Instance": [{"Status": self.status}]}}
        elif name == "ListCdtInternetTraffic":
            if self.traffic_error:
                raise RuntimeError(self.traffic_error)
            payload = {"TrafficDetails": [{"Traffic": self.traffic_gb * 1024**3}]}
        elif name == "StartInstances":
            self.status = "Running"
            payload = {"RequestId": "test-start"}
        elif name == "StopInstances":
            self.status = "Stopped"
            payload = {"RequestId": "test-stop"}
        else:
            raise AssertionError(f"Unexpected request: {name}")
        return json.dumps(payload).encode("utf-8")


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        accounts = {
            "cn": AccountConfig("cn", "阿里云内地", "cn-id", "cn-secret"),
            "intl": AccountConfig("intl", "阿里云国际", "intl-id", "intl-secret"),
        }
        targets = {
            "cn-one": TargetConfig("cn-one", "北京主机", "cn", "cn-beijing", "i-cn-one", 180),
            "intl-one": TargetConfig("intl-one", "香港主机", "intl", "cn-hongkong", "i-intl-one", 200),
        }
        self.config = Config(
            accounts=accounts,
            targets=targets,
            control_token="a" * 32,
            totp_secret="",
            listen_host="127.0.0.1",
            listen_port=8787,
            state_path=root / "state.json",
            lock_dir=root / "locks",
            cookie_secure=False,
            trust_proxy=False,
            session_hours=8,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def fleet(self, cn=None, intl=None):
        return ControlFleet(
            self.config,
            clients={
                "cn-one": cn or FakeClient(),
                "intl-one": intl or FakeClient(),
            },
        )

    def test_start_is_target_specific_and_idempotent(self):
        cn = FakeClient(status="Running")
        intl = FakeClient(status="Stopped")
        result = self.fleet(cn, intl).controller("cn-one").start()
        self.assertFalse(result["changed"])
        self.assertNotIn("StartInstances", cn.calls)
        self.assertEqual(intl.calls, [])

    def test_policy_is_paused_by_default(self):
        client = FakeClient()
        result = self.fleet(cn=client).controller("cn-one").check_policy()
        self.assertTrue(result["skipped"])
        self.assertEqual(client.calls, [])

    def test_force_policy_starts_below_threshold(self):
        client = FakeClient(status="Stopped", traffic_gb=100)
        result = self.fleet(cn=client).controller("cn-one").check_policy(force=True)
        self.assertEqual(result["decision"], "start")
        self.assertIn("StartInstances", client.calls)

    def test_force_policy_stops_at_threshold(self):
        client = FakeClient(status="Running", traffic_gb=180)
        result = self.fleet(cn=client).controller("cn-one").check_policy(force=True)
        self.assertEqual(result["decision"], "stop")
        self.assertIn("StopInstances", client.calls)

    def test_auto_state_is_isolated_between_targets(self):
        fleet = self.fleet()
        fleet.controller("cn-one").set_auto(True)
        self.assertTrue(fleet.state.target(self.config.targets["cn-one"])["auto_enabled"])
        self.assertFalse(fleet.state.target(self.config.targets["intl-one"])["auto_enabled"])

    def test_overview_contains_all_targets_without_secrets(self):
        fleet = self.fleet(
            FakeClient(status="Running", traffic_gb=90),
            FakeClient(status="Stopped", traffic_gb=50),
        )
        result = fleet.overview()
        self.assertEqual(result["target_count"], 2)
        self.assertEqual([item["id"] for item in result["targets"]], ["cn-one", "intl-one"])
        self.assertEqual(result["targets"][0]["usage_percent"], 50.0)
        self.assertNotIn("access_key_secret", json.dumps(result))

    def test_traffic_failure_does_not_disable_power_status(self):
        fleet = self.fleet(cn=FakeClient(status="Running", traffic_error="CDT permission denied"))
        target = fleet.overview()["targets"][0]
        self.assertEqual(target["status"], "Running")
        self.assertIsNone(target["traffic_gb"])
        self.assertEqual(target["traffic_error"], "CDT permission denied")

    def test_unknown_target_is_rejected(self):
        with self.assertRaises(KeyError):
            self.fleet().controller("missing")

    def test_schedule_starts_once_at_configured_local_time(self):
        client = FakeClient(status="Stopped")
        controller = self.fleet(cn=client).controller("cn-one")
        controller.set_schedule(True, "08:00", "23:00", [0], "Asia/Shanghai")
        due = datetime(2026, 8, 3, 0, 0, 15, tzinfo=timezone.utc)
        first = controller.check_schedule(now=due)
        second = controller.check_schedule(now=due)
        self.assertEqual(first["decision"], "start")
        self.assertFalse(first["skipped"])
        self.assertTrue(second["skipped"])
        self.assertEqual(client.calls.count("StartInstances"), 1)

    def test_schedule_stops_at_configured_time(self):
        client = FakeClient(status="Running")
        controller = self.fleet(cn=client).controller("cn-one")
        controller.set_schedule(True, "08:00", "23:00", [0], "Asia/Shanghai")
        result = controller.check_schedule(
            now=datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(result["decision"], "stop")
        self.assertIn("StopInstances", client.calls)

    def test_schedule_skips_unselected_weekday(self):
        client = FakeClient(status="Stopped")
        controller = self.fleet(cn=client).controller("cn-one")
        controller.set_schedule(True, "08:00", "23:00", [1], "Asia/Shanghai")
        result = controller.check_schedule(
            now=datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc)
        )
        self.assertTrue(result["skipped"])
        self.assertEqual(client.calls, [])

    def test_schedule_and_traffic_policy_are_mutually_exclusive(self):
        controller = self.fleet().controller("cn-one")
        controller.set_auto(True)
        with self.assertRaisesRegex(ValueError, "Disable traffic policy"):
            controller.set_schedule(True, "08:00", "23:00", [0], "UTC")
        controller.set_auto(False)
        controller.set_schedule(True, "08:00", "23:00", [0], "UTC")
        with self.assertRaisesRegex(ValueError, "Disable the power schedule"):
            controller.set_auto(True)

    def test_schedule_rejects_invalid_values(self):
        controller = self.fleet().controller("cn-one")
        with self.assertRaisesRegex(ValueError, "HH:MM"):
            controller.set_schedule(True, "8:00", "23:00", [0], "UTC")
        with self.assertRaisesRegex(ValueError, "must differ"):
            controller.set_schedule(True, "08:00", "08:00", [0], "UTC")
        with self.assertRaisesRegex(ValueError, "non-empty"):
            controller.set_schedule(True, "08:00", "23:00", [], "UTC")
        with self.assertRaisesRegex(ValueError, "Unknown schedule timezone"):
            controller.set_schedule(True, "08:00", "23:00", [0], "Mars/Olympus")

    def test_overview_reports_next_schedule_event(self):
        controller = self.fleet().controller("cn-one")
        controller.set_schedule(True, "08:00", "23:00", [0], "UTC")
        state = controller.state.target(controller.target)
        event = controller.next_schedule_event(
            state=state,
            now=datetime(2026, 8, 3, 7, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(event["action"], "start")
        self.assertEqual(event["at"], "2026-08-03T08:00+00:00")

    def test_check_all_reports_both_automation_modes(self):
        result = self.fleet().check_all()
        self.assertEqual(len(result["targets"]), 2)
        self.assertIn("schedule", result["targets"][0])
        self.assertIn("traffic_policy", result["targets"][0])


if __name__ == "__main__":
    unittest.main()
