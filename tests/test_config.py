import json
import tempfile
import unittest
from pathlib import Path

from aliyun_control.config import load_targets_file


class ConfigTests(unittest.TestCase):
    def write_config(self, payload):
        temporary = tempfile.TemporaryDirectory()
        path = Path(temporary.name) / "targets.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return temporary, path

    def base_payload(self):
        return {
            "accounts": [
                {
                    "id": "intl",
                    "name": "Alibaba Cloud International",
                    "access_key_id": "test-id",
                    "access_key_secret": "test-secret",
                }
            ],
            "instances": [
                {
                    "id": "hk-one",
                    "name": "Hong Kong",
                    "account": "intl",
                    "region": "cn-hongkong",
                    "instance_id": "i-test",
                    "traffic_threshold_gb": 180,
                    "auto_enabled_default": False,
                }
            ],
        }

    def test_loads_account_and_target(self):
        temporary, path = self.write_config(self.base_payload())
        try:
            accounts, targets = load_targets_file(path)
            self.assertEqual(accounts["intl"].name, "Alibaba Cloud International")
            self.assertEqual(targets["hk-one"].account_id, "intl")
        finally:
            temporary.cleanup()

    def test_rejects_string_boolean(self):
        payload = self.base_payload()
        payload["instances"][0]["auto_enabled_default"] = "false"
        temporary, path = self.write_config(payload)
        try:
            with self.assertRaisesRegex(ValueError, "must be a boolean"):
                load_targets_file(path)
        finally:
            temporary.cleanup()

    def test_rejects_unknown_account(self):
        payload = self.base_payload()
        payload["instances"][0]["account"] = "missing"
        temporary, path = self.write_config(payload)
        try:
            with self.assertRaisesRegex(ValueError, "unknown account"):
                load_targets_file(path)
        finally:
            temporary.cleanup()

    def test_loads_per_target_schedule_defaults(self):
        payload = self.base_payload()
        payload["instances"][0]["schedule"] = {
            "enabled": True,
            "start": "09:15",
            "stop": "22:30",
            "weekdays": [0, 2, 4],
            "timezone": "Asia/Hong_Kong",
        }
        temporary, path = self.write_config(payload)
        try:
            _, targets = load_targets_file(path)
            target = targets["hk-one"]
            self.assertTrue(target.schedule_enabled_default)
            self.assertEqual(target.schedule_start_default, "09:15")
            self.assertEqual(target.schedule_weekdays_default, (0, 2, 4))
            self.assertEqual(target.schedule_timezone_default, "Asia/Hong_Kong")
        finally:
            temporary.cleanup()

    def test_rejects_unknown_schedule_timezone(self):
        payload = self.base_payload()
        payload["instances"][0]["schedule"] = {"timezone": "Mars/Olympus"}
        temporary, path = self.write_config(payload)
        try:
            with self.assertRaisesRegex(ValueError, "unknown IANA timezone"):
                load_targets_file(path)
        finally:
            temporary.cleanup()

    def test_rejects_schedule_and_traffic_policy_enabled_together(self):
        payload = self.base_payload()
        payload["instances"][0]["auto_enabled_default"] = True
        payload["instances"][0]["schedule"] = {"enabled": True}
        temporary, path = self.write_config(payload)
        try:
            with self.assertRaisesRegex(ValueError, "cannot enable traffic policy and schedule"):
                load_targets_file(path)
        finally:
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
