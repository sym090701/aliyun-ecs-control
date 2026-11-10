import json
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from aliyun_control.config import ManagedConfigStore, load_managed_config, load_targets_file


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

    def test_accepts_fixed_utc_offset_schedule_timezone(self):
        payload = self.base_payload()
        payload["instances"][0]["schedule"] = {"timezone": "UTC+08:00"}
        temporary, path = self.write_config(payload)
        try:
            _, targets = load_targets_file(path)
            self.assertEqual(targets["hk-one"].schedule_timezone_default, "UTC+08:00")
        finally:
            temporary.cleanup()

    def test_allows_schedule_and_traffic_policy_enabled_together(self):
        payload = self.base_payload()
        payload["instances"][0]["auto_enabled_default"] = True
        payload["instances"][0]["schedule"] = {"enabled": True}
        temporary, path = self.write_config(payload)
        try:
            _, targets = load_targets_file(path)
            self.assertTrue(targets["hk-one"].auto_enabled_default)
            self.assertTrue(targets["hk-one"].schedule_enabled_default)
        finally:
            temporary.cleanup()

    def test_rejects_non_finite_traffic_threshold(self):
        payload = self.base_payload()
        payload["instances"][0]["traffic_threshold_gb"] = "NaN"
        temporary, path = self.write_config(payload)
        try:
            with self.assertRaisesRegex(ValueError, "greater than zero"):
                load_targets_file(path)
        finally:
            temporary.cleanup()

    def test_managed_store_encrypts_credentials_and_round_trips(self):
        temporary, legacy_path = self.write_config(self.base_payload())
        try:
            root = Path(temporary.name)
            managed_path = root / "managed.json"
            key_path = root / "credential.key"
            key_path.write_bytes(Fernet.generate_key())
            accounts, targets = load_targets_file(legacy_path)
            ManagedConfigStore(managed_path, key_path).save(accounts, targets)
            raw = managed_path.read_text(encoding="utf-8")
            self.assertNotIn("test-id", raw)
            self.assertNotIn("test-secret", raw)
            loaded_accounts, loaded_targets = load_managed_config(managed_path, key_path)
            self.assertEqual(loaded_accounts["intl"].access_key_secret, "test-secret")
            self.assertEqual(loaded_targets["hk-one"].region_id, "cn-hongkong")
            self.assertEqual(managed_path.stat().st_mode & 0o777, 0o600)
        finally:
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
