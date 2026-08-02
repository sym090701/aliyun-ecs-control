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

    def set_schedule(self, enabled, start_time, stop_time, weekdays, timezone_name):
        if timezone_name == "Mars/Olympus":
            raise ValueError("Unknown schedule timezone: Mars/Olympus")
        return {
            "target_id": self.target_id,
            "schedule_enabled": enabled,
            "schedule_start": start_time,
            "schedule_stop": stop_time,
            "schedule_weekdays": weekdays,
            "schedule_timezone": timezone_name,
        }

    def check_policy(self, force=False):
        return {"target_id": self.target_id, "skipped": False, "forced": force}


class FakeFleet:
    def __init__(self):
        self.controllers = {"cn-one": FakeController("cn-one"), "intl-one": FakeController("intl-one")}

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


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.config = Config(
            accounts={"cn": AccountConfig("cn", "阿里云内地", "test-id", "test-secret")},
            targets={"cn-one": TargetConfig("cn-one", "北京主机", "cn", "cn-beijing", "i-test", 180)},
            control_token="test-control-token-1234567890",
            totp_secret="",
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
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            create_handler(self.config, FakeFleet()),
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

    def login_cookie(self, token=None, totp=None):
        body = {"token": token or self.config.control_token}
        if totp is not None:
            body["totp"] = totp
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

    def test_totp_is_required_when_configured(self):
        secret = pyotp.random_base32()
        self.config = Config(**{**self.config.__dict__, "totp_secret": secret})
        self._restart_server()
        denied, _, _ = self.request("POST", "/api/login", {"token": self.config.control_token})
        accepted, _, _ = self.request(
            "POST",
            "/api/login",
            {"token": self.config.control_token, "totp": pyotp.TOTP(secret).now()},
        )
        self.assertEqual(denied, 401)
        self.assertEqual(accepted, 200)

    def test_secure_cookie_and_hsts_for_https_proxy_mode(self):
        self.config = Config(**{**self.config.__dict__, "cookie_secure": True})
        self._restart_server()
        status, _, headers = self.request(
            "POST", "/api/login", {"token": self.config.control_token}
        )
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
