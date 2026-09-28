"""SOMA Sanctuary — Join The Queue (JTQ) local server."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mailer import mail_config, send_day_report
from store import (
    ARCHIVE_DIR,
    PAYMENT_OPTIONS,
    PRICE_PER_MINUTE,
    SESSION_OPTIONS,
    QueueStore,
    add_client,
    attach_estimates,
    business_date,
    empty_state,
    estimate_for,
    first_name,
    format_phone,
    normalize_phone,
    now,
    set_status,
    waiting_clients,
)

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv()

HOST = os.environ.get("JTQ_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT") or os.environ.get("JTQ_PORT", "8765"))
ADMIN_PASSWORD = os.environ.get("JTQ_ADMIN_PASSWORD", "sanctuary")
CLOSE_HOUR = int(os.environ.get("JTQ_CLOSE_HOUR", "21"))
SESSION_TTL = 12 * 60 * 60


def public_mode() -> bool:
    flag = os.environ.get("JTQ_PUBLIC", "").strip().lower()
    if flag in ("1", "true", "yes"):
        return True
    if flag in ("0", "false", "no"):
        return False
    return bool(
        os.environ.get("RENDER")
        or os.environ.get("RAILWAY_ENVIRONMENT")
        or os.environ.get("FLY_APP_NAME")
    )

store = QueueStore()
sessions: dict[str, float] = {}
sessions_lock = threading.Lock()
email_lock = threading.Lock()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_session() -> str:
    token = secrets.token_urlsafe(24)
    with sessions_lock:
        sessions[hash_token(token)] = time.time() + SESSION_TTL
    return token


def valid_session(token: str | None) -> bool:
    if not token:
        return False
    key = hash_token(token)
    with sessions_lock:
        expiry = sessions.get(key)
        if not expiry:
            return False
        if expiry < time.time():
            sessions.pop(key, None)
            return False
        return True


def drop_session(token: str | None) -> None:
    if not token:
        return
    with sessions_lock:
        sessions.pop(hash_token(token), None)


def constant_time_password(candidate: str) -> bool:
    left = hashlib.sha256(candidate.encode("utf-8")).digest()
    right = hashlib.sha256(ADMIN_PASSWORD.encode("utf-8")).digest()
    return hmac.compare_digest(left, right)


def persist_archive(state: dict, suffix: str = "") -> None:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    day = state.get("day", "day")
    name = f"{day}{suffix}.json"
    (ARCHIVE_DIR / name).write_text(json.dumps(state, indent=2), encoding="utf-8")


def maybe_email_rolled(previous: dict | None) -> None:
    if not previous or previous.get("email_sent"):
        return
    with email_lock:
        ok, _detail = send_day_report(previous)
        if ok:
            previous["email_sent"] = True
            persist_archive(previous)


def public_admin_row(row: dict) -> dict:
    out = dict(row)
    out["phone_display"] = format_phone(row.get("phone", ""))
    return out


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PUBLIC), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{now().strftime('%H:%M:%S')}] {fmt % args}")

    def _session_token(self) -> str | None:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get("jtq_admin")
        return morsel.value if morsel else None

    def _secure_cookie(self) -> bool:
        proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        if proto == "https":
            return True
        return os.environ.get("JTQ_SECURE_COOKIES", "").strip().lower() in ("1", "true", "yes")

    def _admin_cookie(self, token: str | None) -> str:
        if token:
            parts = [f"jtq_admin={token}", "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={SESSION_TTL}"]
        else:
            parts = ["jtq_admin=", "Path=/", "HttpOnly", "Max-Age=0"]
        if self._secure_cookie():
            parts.append("Secure")
        return "; ".join(parts)

    def _json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _send(self, code: int, payload: dict, headers: list[tuple[str, str]] | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers or []:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, message: str) -> None:
        self._send(code, {"error": message})

    def _require_admin(self) -> bool:
        if valid_session(self._session_token()):
            return True
        self._error(401, "Admin sign-in required.")
        return False

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        routes = {
            "/api/health": self.api_health,
            "/api/meta": self.api_meta,
            "/api/check": self.api_check,
            "/api/display": self.api_display,
            "/api/admin/queue": self.api_admin_queue,
        }
        if path in routes:
            routes[path]()
            return
        aliases = {
            "/": "/join.html",
            "/join": "/join.html",
            "/check": "/check.html",
            "/display": "/display.html",
            "/admin": "/admin.html",
        }
        if path in aliases:
            self.path = aliases[path]
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        routes = {
            "/api/join": self.api_join,
            "/api/admin/login": self.api_login,
            "/api/admin/logout": self.api_logout,
            "/api/admin/done": self.api_done,
            "/api/admin/remove": self.api_remove,
            "/api/admin/display": self.api_set_display,
            "/api/admin/end-day": self.api_end_day,
        }
        handler = routes.get(path)
        if not handler:
            self._error(404, "Unknown endpoint.")
            return
        try:
            handler()
        except json.JSONDecodeError:
            self._error(400, "Invalid JSON.")

    def api_health(self) -> None:
        self._send(200, {"ok": True, "service": "jtq"})

    def api_meta(self) -> None:
        self._send(
            200,
            {
                "business": "SOMA Sanctuary",
                "product": "Join The Queue",
                "short": "JTQ",
                "site": "https://www.somasanctuary.nyc",
                "sessions": list(SESSION_OPTIONS),
                "payments": list(PAYMENT_OPTIONS),
                "buffer_minutes": 5,
                "price_per_minute": PRICE_PER_MINUTE,
            },
        )

    def api_join(self) -> None:
        body = self._json_body()
        name = str(body.get("name") or "").strip()
        phone = normalize_phone(str(body.get("phone") or ""))
        payment = str(body.get("payment") or "").strip()
        try:
            session_minutes = int(body.get("session_minutes"))
        except (TypeError, ValueError):
            self._error(400, "Choose a session length.")
            return
        if len(name) < 2:
            self._error(400, "Please enter your name.")
            return
        if len(phone) != 10:
            self._error(400, "Enter a 10-digit US phone number.")
            return
        if session_minutes not in SESSION_OPTIONS:
            self._error(400, "That session length is not offered.")
            return
        if payment not in PAYMENT_OPTIONS:
            self._error(400, "Choose a payment method.")
            return

        def op(state):
            waiting = waiting_clients(state)
            for existing in waiting:
                if existing["phone"] == phone:
                    est = estimate_for(state, existing)
                    return {
                        "already_in_line": True,
                        "id": existing["id"],
                        "name": existing["name"],
                        "return_label": est["return_label"],
                        "wait_minutes": est["wait_minutes"],
                        "message": (
                            "You're already in line. Please return around "
                            f"{est['return_label']}."
                        ),
                    }
            client = add_client(state, name, phone, session_minutes, payment)
            est = estimate_for(state, client)
            return {
                "already_in_line": False,
                "id": client["id"],
                "name": client["name"],
                "return_label": est["return_label"],
                "wait_minutes": est["wait_minutes"],
                "message": (
                    "You're next — please stay nearby."
                    if est["wait_minutes"] == 0
                    else f"Please return around {est['return_label']}."
                ),
            }

        result, rolled = store.mutate(op)
        maybe_email_rolled(rolled)
        self._send(200, result)

    def api_check(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        phone = normalize_phone((query.get("phone") or [""])[0])
        if len(phone) != 10:
            self._error(400, "Enter a 10-digit US phone number.")
            return
        state, rolled = store.snapshot()
        maybe_email_rolled(rolled)
        matches = [c for c in state["clients"] if c["phone"] == phone]
        if not matches:
            self._error(404, "We don't have that number in today's queue.")
            return
        client = matches[-1]
        if client["status"] == "waiting":
            est = estimate_for(state, client)
            if est["wait_minutes"] == 0:
                message = "You're next — please stay nearby."
            else:
                message = f"Please return around {est['return_label']}."
            self._send(
                200,
                {
                    "name": first_name(client["name"]),
                    "status": "waiting",
                    "return_label": est["return_label"],
                    "wait_minutes": est["wait_minutes"],
                    "message": message,
                },
            )
            return
        if client["status"] == "done":
            self._send(
                200,
                {
                    "name": first_name(client["name"]),
                    "status": "done",
                    "return_label": None,
                    "message": "Your session is finished. Thank you for visiting SOMA Sanctuary.",
                },
            )
            return
        self._send(
            200,
            {
                "name": first_name(client["name"]),
                "status": "removed",
                "return_label": None,
                "message": "You're no longer in line. Join The Queue again if you'd still like a session.",
            },
        )

    def api_display(self) -> None:
        state, rolled = store.snapshot()
        maybe_email_rolled(rolled)
        if not state.get("display_enabled", False):
            self._send(200, {"enabled": False, "clients": []})
            return
        rows = [
            {
                "first_name": row["first_name"],
                "return_label": row["return_label"],
                "wait_minutes": row["wait_minutes"],
            }
            for row in attach_estimates(state)
            if row["status"] == "waiting"
        ]
        self._send(200, {"enabled": True, "clients": rows})

    def api_admin_queue(self) -> None:
        if not self._require_admin():
            return
        state, rolled = store.snapshot()
        maybe_email_rolled(rolled)
        rows = attach_estimates(state)
        waiting = [public_admin_row(r) for r in rows if r["status"] == "waiting"]
        others = [public_admin_row(r) for r in rows if r["status"] != "waiting"]
        cfg = mail_config()
        self._send(
            200,
            {
                "day": state["day"],
                "display_enabled": state.get("display_enabled", False),
                "waiting": waiting,
                "others": others,
                "email_configured": cfg["ready"],
                "email_to": cfg["to"],
            },
        )

    def api_login(self) -> None:
        body = self._json_body()
        password = str(body.get("password") or "")
        if not constant_time_password(password):
            self._error(401, "That password didn't match.")
            return
        token = new_session()
        self._send(200, {"ok": True}, [("Set-Cookie", self._admin_cookie(token))])

    def api_logout(self) -> None:
        drop_session(self._session_token())
        self._send(200, {"ok": True}, [("Set-Cookie", self._admin_cookie(None))])

    def api_done(self) -> None:
        if not self._require_admin():
            return
        body = self._json_body()
        client_id = str(body.get("id") or "")

        def op(state):
            return set_status(state, client_id, "done")

        client, rolled = store.mutate(op)
        maybe_email_rolled(rolled)
        if not client:
            self._error(404, "Client not found.")
            return
        self._send(200, {"ok": True})

    def api_remove(self) -> None:
        if not self._require_admin():
            return
        body = self._json_body()
        client_id = str(body.get("id") or "")

        def op(state):
            return set_status(state, client_id, "removed")

        client, rolled = store.mutate(op)
        maybe_email_rolled(rolled)
        if not client:
            self._error(404, "Client not found.")
            return
        self._send(200, {"ok": True})

    def api_set_display(self) -> None:
        if not self._require_admin():
            return
        body = self._json_body()
        enabled = bool(body.get("enabled"))

        def op(state):
            state["display_enabled"] = enabled
            return enabled

        enabled, rolled = store.mutate(op)
        maybe_email_rolled(rolled)
        self._send(200, {"display_enabled": enabled})

    def api_end_day(self) -> None:
        if not self._require_admin():
            return

        def op(state):
            snapshot = json.loads(json.dumps(state))
            ok, detail = send_day_report(snapshot)
            snapshot["email_sent"] = ok
            persist_archive(snapshot, "-end")
            new_state = empty_state(business_date())
            state.clear()
            state.update(new_state)
            return {"emailed": ok, "detail": detail, "count": len(snapshot.get("clients", []))}

        result, rolled = store.mutate(op)
        maybe_email_rolled(rolled)
        self._send(200, result)


def lan_hint() -> str:
    import socket

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "127.0.0.1"


def auto_close_loop() -> None:
    while True:
        time.sleep(30)
        try:
            if now().hour < CLOSE_HOUR:
                continue

            def op(state):
                if state.get("email_sent") or not state.get("clients"):
                    return None
                snapshot = json.loads(json.dumps(state))
                ok, detail = send_day_report(snapshot)
                if ok:
                    state["email_sent"] = True
                    persist_archive(snapshot, "-close")
                return {"emailed": ok, "detail": detail}

            result, rolled = store.mutate(op)
            maybe_email_rolled(rolled)
            if result and result.get("emailed"):
                print(f"End-of-day email sent: {result.get('detail')}")
        except Exception as exc:  # noqa: BLE001
            print(f"Auto-close email error: {exc}")


def main() -> None:
    if public_mode() and ADMIN_PASSWORD == "sanctuary":
        raise SystemExit(
            "Refusing to start on the public internet with the default admin password. "
            "Set JTQ_ADMIN_PASSWORD in the host dashboard (do not put it in git)."
        )
    PUBLIC.mkdir(exist_ok=True)
    threading.Thread(target=auto_close_loop, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("SOMA Sanctuary — Join The Queue (JTQ)")
    print(f"  Listening on {HOST}:{PORT}")
    if public_mode():
        print("  Public Join/Check/Display are open. Admin stays cookie-protected.")
        print("  Admin password is set via JTQ_ADMIN_PASSWORD (not printed).")
    else:
        ip = lan_hint()
        print(f"  Local:  http://127.0.0.1:{PORT}/join")
        print(f"  LAN:    http://{ip}:{PORT}/join")
        print(f"  Check:  http://127.0.0.1:{PORT}/check")
        print(f"  Board:  http://127.0.0.1:{PORT}/display")
        print(f"  Admin:  http://127.0.0.1:{PORT}/admin")
        print(f"  Admin password: {ADMIN_PASSWORD}  (change with JTQ_ADMIN_PASSWORD)")
    print("  End-of-day email: somasanctuarynyc@gmail.com (set JTQ_SMTP_PASS only on the host, never in git)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nJTQ stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
