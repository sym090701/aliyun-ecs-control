from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import mimetypes
import secrets
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, unquote, urlparse

import pyotp

from .config import Config
from .service import ControlFleet

LOG = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"


class LoginLimiter:
    def __init__(self) -> None:
        self._attempts: Dict[str, list] = {}
        self._lock = threading.Lock()

    def allowed(self, address: str) -> bool:
        now = time.monotonic()
        with self._lock:
            attempts = [item for item in self._attempts.get(address, []) if now - item < 60]
            self._attempts[address] = attempts
            return len(attempts) < 6

    def failed(self, address: str) -> None:
        with self._lock:
            self._attempts.setdefault(address, []).append(time.monotonic())

    def succeeded(self, address: str) -> None:
        with self._lock:
            self._attempts.pop(address, None)


class SessionStore:
    def __init__(self, lifetime_seconds: int) -> None:
        self.lifetime_seconds = lifetime_seconds
        self._sessions: Dict[str, float] = {}
        self._lock = threading.Lock()

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._purge_locked()
            self._sessions[token] = time.monotonic() + self.lifetime_seconds
        return token

    def valid(self, token: str) -> bool:
        if not token:
            return False
        with self._lock:
            self._purge_locked()
            expires = self._sessions.get(token, 0)
            return expires > time.monotonic()

    def revoke(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def _purge_locked(self) -> None:
        now = time.monotonic()
        expired = [token for token, expires in self._sessions.items() if expires <= now]
        for token in expired:
            self._sessions.pop(token, None)


def create_handler(config: Config, fleet: ControlFleet):
    limiter = LoginLimiter()
    sessions = SessionStore(config.session_hours * 3600)
    totp = pyotp.TOTP(config.totp_secret) if config.totp_secret else None
    if totp is None:
        raise ValueError("TOTP_SECRET must be configured for web login")

    class Handler(BaseHTTPRequestHandler):
        server_version = "ECSControl/2.2"

        def log_message(self, fmt: str, *args: Any) -> None:
            LOG.info("%s - %s", self._client_ip(), fmt % args)

        def _client_ip(self) -> str:
            peer = self.client_address[0]
            if not config.trust_proxy:
                return peer
            try:
                if not ipaddress.ip_address(peer).is_loopback:
                    return peer
            except ValueError:
                return peer
            forwarded = self.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
            try:
                return str(ipaddress.ip_address(forwarded)) if forwarded else peer
            except ValueError:
                return peer

        def _json_body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 16_384:
                raise ValueError("Request body length is invalid")
            if length == 0:
                return {}
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            return payload

        def _security_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if config.cookie_secure:
                self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")

        def _send_json(self, status: int, payload: Dict[str, Any], cookie: Optional[str] = None) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self._security_headers()
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(data)

        def _send_file(self, path: Path) -> None:
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self._security_headers()
            self.end_headers()
            self.wfile.write(data)

        def _session_token(self) -> str:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            morsel = cookie.get("ecs_control_session")
            return morsel.value if morsel else ""

        def _totp_valid(self, value: object) -> bool:
            if totp is None:
                return True
            code = str(value or "").strip()
            return len(code) == 6 and code.isdigit() and bool(totp.verify(code, valid_window=1))

        def _authenticated(self) -> bool:
            return sessions.valid(self._session_token())

        def _same_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                return False
            parsed = urlparse(origin)
            return parsed.netloc == self.headers.get("Host", "") and parsed.scheme in {"http", "https"}

        def _require_auth(self, write: bool = False) -> bool:
            if not self._authenticated():
                self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Authentication required"})
                return False
            if write and not self._same_origin():
                self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Origin check failed"})
                return False
            return True

        def _require_step_up(self, body: Dict[str, Any]) -> bool:
            if totp is None:
                self._send_json(
                    HTTPStatus.FORBIDDEN,
                    {"ok": False, "error": "TOTP must be configured for account and instance management"},
                )
                return False
            if not self._totp_valid(body.get("totp")):
                self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "A current TOTP code is required"})
                return False
            body.pop("totp", None)
            return True

        def _target_route(self, path: str):
            parts = [unquote(item) for item in path.strip("/").split("/")]
            if len(parts) == 4 and parts[:2] == ["api", "targets"]:
                return parts[2], parts[3]
            return None

        @staticmethod
        def _resource_route(path: str):
            parts = [unquote(item) for item in path.strip("/").split("/")]
            if len(parts) == 2 and parts[0] == "api" and parts[1] in {"accounts", "instances"}:
                return parts[1], None
            if len(parts) == 3 and parts[0] == "api" and parts[1] in {"accounts", "instances"}:
                return parts[1], parts[2]
            return None

        def do_GET(self) -> None:
            parsed_url = urlparse(self.path)
            path = parsed_url.path
            if path in {"/", "/index.html"}:
                self._send_file(STATIC_DIR / "index.html")
            elif path == "/app.css":
                self._send_file(STATIC_DIR / "app.css")
            elif path == "/app.js":
                self._send_file(STATIC_DIR / "app.js")
            elif path == "/api/session":
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "authenticated": self._authenticated()},
                )
            elif path == "/api/overview":
                if self._require_auth():
                    self._handle(fleet.overview)
            elif path == "/api/configuration":
                if self._require_auth():
                    self._handle(fleet.configuration)
            elif (route := self._target_route(path)) is not None and route[1] == "activity":
                if not self._require_auth():
                    return
                target_id, _ = route
                try:
                    controller = fleet.controller(target_id)
                except KeyError as exc:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
                    return
                query = parse_qs(parsed_url.query, keep_blank_values=True)
                try:
                    source = query.get("source", ["all"])[0]
                    outcome = query.get("outcome", ["all"])[0]
                    offset = int(query.get("offset", ["0"])[0])
                    limit = int(query.get("limit", ["50"])[0])
                except ValueError:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "activity pagination is invalid"})
                    return
                self._handle(lambda: controller.activity_history(source, outcome, offset, limit))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            try:
                body = self._json_body()
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return

            if path == "/api/login":
                self._login(body)
                return
            if path == "/api/logout":
                sessions.revoke(self._session_token())
                cookie = "ecs_control_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
                self._send_json(HTTPStatus.OK, {"ok": True}, cookie=cookie)
                return
            if not self._require_auth(write=True):
                return

            resource_route = self._resource_route(path)
            if path == "/api/credentials/test":
                if self._require_step_up(body):
                    self._handle(lambda: fleet.test_credentials(body))
                return
            if resource_route is not None:
                if not self._require_step_up(body):
                    return
                resource, resource_id = resource_route
                actor = self._client_ip()
                if resource == "accounts" and resource_id is None:
                    self._handle(lambda: fleet.add_account(body, actor=actor))
                elif resource == "accounts":
                    self._handle(lambda: fleet.update_account(resource_id, body, actor=actor))
                elif resource == "instances" and resource_id is None:
                    self._handle(lambda: fleet.add_target(body, actor=actor))
                else:
                    self._handle(lambda: fleet.update_target(resource_id, body, actor=actor))
                return

            route = self._target_route(path)
            if route is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            target_id, action = route
            try:
                controller = fleet.controller(target_id)
            except KeyError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
                return

            if action == "start":
                self._handle(controller.start)
            elif action == "stop":
                if body.get("confirm") != "STOP":
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Stop confirmation is required"})
                    return
                self._handle(controller.stop)
            elif action == "auto":
                enabled = body.get("enabled")
                if not isinstance(enabled, bool):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "enabled must be a boolean"})
                    return
                self._handle(lambda: controller.set_auto(enabled))
            elif action == "schedule":
                enabled = body.get("enabled")
                if not isinstance(enabled, bool):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "enabled must be a boolean"})
                    return
                self._handle(
                    lambda: controller.set_schedule(
                        enabled=enabled,
                        start_time=body.get("start"),
                        stop_time=body.get("stop"),
                        weekdays=body.get("weekdays"),
                        timezone_name=body.get("timezone"),
                        days=body.get("days"),
                    )
                )
            elif action == "threshold":
                self._handle(
                    lambda: fleet.set_threshold(
                        target_id,
                        body.get("threshold_gb"),
                        actor=self._client_ip(),
                    )
                )
            elif action == "check":
                self._handle(lambda: controller.check_automation(force_traffic=True))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def do_DELETE(self) -> None:
            path = urlparse(self.path).path
            try:
                body = self._json_body()
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return
            if not self._require_auth(write=True) or not self._require_step_up(body):
                return
            route = self._resource_route(path)
            if route is None or route[1] is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            resource, resource_id = route
            actor = self._client_ip()
            if resource == "accounts":
                self._handle(lambda: fleet.delete_account(resource_id, actor=actor))
            else:
                self._handle(lambda: fleet.delete_target(resource_id, actor=actor))

        def _login(self, body: Dict[str, Any]) -> None:
            address = self._client_ip()
            if not limiter.allowed(address):
                self._send_json(HTTPStatus.TOO_MANY_REQUESTS, {"ok": False, "error": "Too many login attempts"})
                return
            valid = self._totp_valid(body.get("totp"))
            if not valid:
                limiter.failed(address)
                self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Invalid credentials"})
                return
            limiter.succeeded(address)
            session = sessions.create()
            secure = "; Secure" if config.cookie_secure else ""
            cookie = f"ecs_control_session={session}; Path=/; HttpOnly; SameSite=Strict; Max-Age={config.session_hours * 3600}{secure}"
            self._send_json(HTTPStatus.OK, {"ok": True}, cookie=cookie)

        def _handle(self, action) -> None:
            try:
                self._send_json(HTTPStatus.OK, {"ok": True, "data": action()})
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            except KeyError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
            except Exception as exc:
                LOG.exception("Request failed")
                self._send_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": str(exc)})

    return Handler


def serve(config: Config, fleet: Optional[ControlFleet] = None) -> None:
    fleet = fleet or ControlFleet(config)
    server = ThreadingHTTPServer(
        (config.listen_host, config.listen_port),
        create_handler(config, fleet),
    )
    LOG.info("Listening on http://%s:%s", config.listen_host, config.listen_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
