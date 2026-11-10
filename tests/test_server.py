import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

import pyotp

from aliyun_control.config import AccountConfig, Config, TargetConfig
from aliyun_control.server import create_handler


class FakeController:
    def __init__(self, target_id):
        self.target_id = target_id

    def start(self):
        return {"target_id": self.target_id, "changed": True, "status": "Starting"}

    def stop(self):
        return {"target_id": self.target_id, "changed": True, "status": "Stopping"}

    def set_auto(self, enabled):
        return {"target_id": self.target_id, "auto_enabled": enabled}

    def set_schedule(self, enabled, start_time=None, stop_time=None, weekdays=None, timezone_name=None, days=None):
        if timezone_name == "Mars/Olympus":
            raise ValueError("Unknown schedule timezone: Mars/Olympus")
        return {
            "target_id": self.target_id,
            "schedule_enabled": enabled,
            "schedule_start": start_time,
            "schedule_stop": stop_time,
            "schedule_weekdays": weekdays,
            "schedule_timezone": timezone_name,
            "schedule_days": days,
        }

    def activity_history(self, source="all", outcome="all", offset=0, limit=50):
        items = [
            {"at": "2026-08-13T00:00:00+00:00", "action": "traffic_start", "result": "Start request accepted", "outcome": "success"},
            {"at": "2026-08-12T00:00:00+00:00", "action": "schedule_stop", "result": "Graceful stop request accepted", "outcome": "success"},
        ]
        return {"items": items[offset:offset + limit], "offset": offset, "limit": limit, "has_more": False}

    def check_policy(self, force=False):
        return {"target_id": self.target_id, "skipped": False, "forced": force}

    def check_automation(self, force_traffic=False):
        return {"target_id": self.target_id, "skipped": False, "forced": force_traffic}


class FakeFleet:
    def __init__(self):
        self.controllers = {"cn-one": FakeController("cn-one"), "intl-one": FakeController("intl-one")}
        self.last_management_call = None

    def overview(self):
        return {
            "target_count": 2,
            "targets": [
                {"id": "cn-one", "name": "北京主机", "status": "Running"},
                {"id": "intl-one", "name": "香港主机", "status": "Stopped"},
            ],
        }

    def controller(self, target_id):
        if target_id not in self.controllers:
            raise KeyError(f"Unknown target: {target_id}")
        return self.controllers[target_id]

    def configuration(self):
        return {
            "accounts": [{"id": "cn", "name": "阿里云内地", "access_key_id_masked": "test...-id", "instance_count": 1}],
            "instances": [{"id": "cn-one", "name": "北京主机", "account": "cn"}],
        }

    def add_account(self, body, actor=""):
        self.last_management_call = ("add_account", dict(body))
        return {"id": body["id"], "name": body["name"], "access_key_id_masked": "****", "instance_count": 0}

    def update_account(self, account_id, body, actor=""):
        self.last_management_call = ("update_account", account_id, dict(body))
        return {"id": account_id, "name": body["name"], "access_key_id_masked": "****", "instance_count": 0}

    def delete_account(self, account_id, actor=""):
        self.last_management_call = ("delete_account", account_id)
        return {"id": account_id, "deleted": True}

    def add_target(self, body, actor=""):
        self.last_management_call = ("add_target", dict(body))
        return {"id": body["id"]}

    def update_target(self, target_id, body, actor=""):
        self.last_management_call = ("update_target", target_id, dict(body))
        return {"id": target_id}

    def set_threshold(self, target_id, value, actor=""):
        self.last_management_call = ("set_threshold", target_id, value, actor)
        return {"target_id": target_id, "threshold_gb": float(value)}

    def delete_target(self, target_id, actor=""):
        self.last_management_call = ("delete_target", target_id)
        return {"id": target_id, "deleted": True}

    def test_credentials(self, body):
        self.last_management_call = ("test_credentials", dict(body))
        return {"valid": True}


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.config = Config(
            accounts={"cn": AccountConfig("cn", "阿里云内地", "test-id", "test-secret")},
            targets={"cn-one": TargetConfig("cn-one", "北京主机", "cn", "cn-beijing", "i-test", 180)},
            control_token="test-control-token-1234567890",
            totp_secret=pyotp.random_base32(),
            listen_host="127.0.0.1",
            listen_port=0,
            state_path=root / "state.json",
            lock_dir=root / "locks",
            cookie_secure=False,
            trust_proxy=False,
            session_hours=8,
        )
        self._start_server()

    def _start_server(self):
        self.fleet = FakeFleet()
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            create_handler(self.config, self.fleet),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def _restart_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self._start_server()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        connection.request(method, path, body=payload, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        data = json.loads(raw.decode("utf-8")) if raw else {}
        response_headers = dict(response.getheaders())
        connection.close()
        return response.status, data, response_headers

    def login_cookie(self, totp=None):
        body = {"totp": totp or pyotp.TOTP(self.config.totp_secret).now()}
        status, _, headers = self.request("POST", "/api/login", body)
        self.assertEqual(status, 200)
        return headers["Set-Cookie"].split(";", 1)[0]

    def write_headers(self, cookie):
        host = f"127.0.0.1:{self.port}"
        return {"Cookie": cookie, "Origin": f"http://{host}", "Host": host}

    def test_overview_requires_authentication(self):
        status, data, _ = self.request("GET", "/api/overview")
        self.assertEqual(status, 401)
        self.assertFalse(data["ok"])

    def test_login_uses_random_session_cookie(self):
        first = self.login_cookie()
        second = self.login_cookie()
        self.assertNotEqual(first, second)
        self.assertNotIn(self.config.control_token, first)

    def test_login_and_read_multi_target_overview(self):
        cookie = self.login_cookie()
        status, data, _ = self.request("GET", "/api/overview", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertEqual(data["data"]["target_count"], 2)

    def test_configuration_requires_authentication(self):
        status, _, _ = self.request("GET", "/api/configuration")
        self.assertEqual(status, 401)

    def test_account_management_requires_current_totp_and_strips_code(self):
        secret = pyotp.random_base32()
        self.config = Config(**{**self.config.__dict__, "totp_secret": secret})
        self._restart_server()
        code = pyotp.TOTP(secret).now()
        cookie = self.login_cookie(totp=code)
        body = {"id": "new", "name": "New", "access_key_id": "test-id", "access_key_secret": "test-secret"}
        denied, _, _ = self.request("POST", "/api/accounts", body, self.write_headers(cookie))
        accepted, data, _ = self.request("POST", "/api/accounts", {**body, "totp": code}, self.write_headers(cookie))
        self.assertEqual(denied, 403)
        self.assertEqual(accepted, 200)
        self.assertNotIn("totp", self.fleet.last_management_call[1])
        self.assertNotIn("access_key_secret", data["data"])

    def test_instance_create_and_delete_routes_require_step_up(self):
        secret = pyotp.random_base32()
        self.config = Config(**{**self.config.__dict__, "totp_secret": secret})
        self._restart_server()
        code = pyotp.TOTP(secret).now()
        cookie = self.login_cookie(totp=code)
        headers = self.write_headers(cookie)
        created, _, _ = self.request(
            "POST",
            "/api/instances",
            {
                "id": "hk-two",
                "name": "HK Two",
                "account": "cn",
                "region": "cn-hongkong",
                "instance_id": "i-two",
                "traffic_threshold_gb": 100,
                "totp": code,
            },
            headers,
        )
        deleted, _, _ = self.request(
            "DELETE", "/api/instances/hk-two", {"totp": code}, headers
        )
        self.assertEqual(created, 200)
        self.assertEqual(deleted, 200)
        self.assertEqual(self.fleet.last_management_call, ("delete_target", "hk-two"))

    def test_login_requires_only_current_totp(self):
        denied, _, _ = self.request("POST", "/api/login", {})
        accepted, _, _ = self.request(
            "POST",
            "/api/login",
            {"totp": pyotp.TOTP(self.config.totp_secret).now()},
        )
        self.assertEqual(denied, 401)
        self.assertEqual(accepted, 200)

    def test_bearer_token_cannot_bypass_totp_login(self):
        status, _, _ = self.request(
            "GET",
            "/api/overview",
            headers={"Authorization": f"Bearer {self.config.control_token}", "X-TOTP": pyotp.TOTP(self.config.totp_secret).now()},
        )
        self.assertEqual(status, 401)

    def test_secure_cookie_and_hsts_for_https_proxy_mode(self):
        self.config = Config(**{**self.config.__dict__, "cookie_secure": True})
        self._restart_server()
        status, _, headers = self.request("POST", "/api/login", {"totp": pyotp.TOTP(self.config.totp_secret).now()})
        self.assertEqual(status, 200)
        self.assertIn("Secure", headers["Set-Cookie"])
        self.assertIn("max-age=31536000", headers["Strict-Transport-Security"])

    def test_cookie_write_requires_same_origin(self):
        cookie = self.login_cookie()
        status, _, _ = self.request("POST", "/api/targets/cn-one/auto", {"enabled": True}, {"Cookie": cookie})
        self.assertEqual(status, 403)

    def test_target_route_controls_selected_instance(self):
        cookie = self.login_cookie()
        status, data, _ = self.request(
            "POST", "/api/targets/intl-one/start", {}, self.write_headers(cookie)
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["data"]["target_id"], "intl-one")

    def test_auto_requires_boolean(self):
        cookie = self.login_cookie()
        status, _, _ = self.request(
            "POST", "/api/targets/cn-one/auto", {"enabled": "false"}, self.write_headers(cookie)
        )
        self.assertEqual(status, 400)

    def test_threshold_route_updates_selected_target(self):
        cookie = self.login_cookie()
        status, data, _ = self.request(
            "POST",
            "/api/targets/intl-one/threshold",
            {"threshold_gb": 215.5},
            self.write_headers(cookie),
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["data"], {"target_id": "intl-one", "threshold_gb": 215.5})
        self.assertEqual(self.fleet.last_management_call[:3], ("set_threshold", "intl-one", 215.5))

    def test_stop_requires_confirmation(self):
        cookie = self.login_cookie()
        headers = self.write_headers(cookie)
        denied, _, _ = self.request("POST", "/api/targets/cn-one/stop", {}, headers)
        accepted, _, _ = self.request("POST", "/api/targets/cn-one/stop", {"confirm": "STOP"}, headers)
        self.assertEqual(denied, 400)
        self.assertEqual(accepted, 200)

    def test_unknown_target_returns_not_found(self):
        cookie = self.login_cookie()
        status, _, _ = self.request(
            "POST", "/api/targets/missing/start", {}, self.write_headers(cookie)
        )
        self.assertEqual(status, 404)

    def test_schedule_route_updates_selected_target(self):
        cookie = self.login_cookie()
        status, data, _ = self.request(
            "POST",
            "/api/targets/intl-one/schedule",
            {
                "enabled": True,
                "start": "09:15",
                "stop": "22:30",
                "weekdays": [0, 1, 2, 3, 4],
                "timezone": "Asia/Hong_Kong",
            },
            self.write_headers(cookie),
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["data"]["target_id"], "intl-one")
        self.assertEqual(data["data"]["schedule_start"], "09:15")

    def test_schedule_route_accepts_independent_daily_times(self):
        cookie = self.login_cookie()
        days = [
            {"weekday": day, "enabled": day < 2, "start": "08:00" if day == 0 else "10:30", "stop": "18:00" if day == 0 else "20:15"}
            for day in range(7)
        ]
        status, data, _ = self.request(
            "POST",
            "/api/targets/intl-one/schedule",
            {"enabled": True, "days": days, "timezone": "Asia/Hong_Kong"},
            self.write_headers(cookie),
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["data"]["schedule_days"], days)

    def test_activity_route_requires_authentication_and_returns_page(self):
        denied, _, _ = self.request("GET", "/api/targets/cn-one/activity")
        cookie = self.login_cookie()
        accepted, data, _ = self.request(
            "GET", "/api/targets/cn-one/activity?source=traffic&outcome=success&offset=0&limit=1",
            headers={"Cookie": cookie},
        )
        self.assertEqual(denied, 401)
        self.assertEqual(accepted, 200)
        self.assertEqual(len(data["data"]["items"]), 1)
        self.assertEqual(data["data"]["limit"], 1)

    def test_schedule_requires_boolean_enabled(self):
        cookie = self.login_cookie()
        status, _, _ = self.request(
            "POST",
            "/api/targets/cn-one/schedule",
            {"enabled": "true"},
            self.write_headers(cookie),
        )
        self.assertEqual(status, 400)

    def test_schedule_validation_error_returns_bad_request(self):
        cookie = self.login_cookie()
        status, data, _ = self.request(
            "POST",
            "/api/targets/cn-one/schedule",
            {
                "enabled": True,
                "start": "08:00",
                "stop": "23:00",
                "weekdays": [0],
                "timezone": "Mars/Olympus",
            },
            self.write_headers(cookie),
        )
        self.assertEqual(status, 400)
        self.assertIn("Unknown schedule timezone", data["error"])


if __name__ == "__main__":
    unittest.main()
