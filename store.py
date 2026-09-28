"""JSON-backed queue store with wait estimates."""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import tzdata  # noqa: F401 — IANA zones on Windows
except ImportError:
    tzdata = None

BUFFER_MINUTES = 5
PRICE_PER_MINUTE = 3
TZ = ZoneInfo("America/New_York")
SESSION_OPTIONS = (5, 10, 15, 20)
PAYMENT_OPTIONS = ("Cash", "Card", "Venmo", "Zelle", "Other")


def _load_dotenv() -> None:
    path = Path(__file__).resolve().parent / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _data_dir() -> Path:
    override = os.environ.get("JTQ_DATA_DIR", "").strip()
    return Path(override) if override else Path(__file__).resolve().parent / "data"


DATA_DIR = _data_dir()
DATA_FILE = DATA_DIR / "queue.json"
ARCHIVE_DIR = DATA_DIR / "archive"


def now() -> datetime:
    return datetime.now(TZ)


def business_date(dt: datetime | None = None) -> str:
    return (dt or now()).date().isoformat()


def normalize_phone(raw: str) -> str:
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def first_name(full_name: str) -> str:
    parts = (full_name or "").strip().split()
    return parts[0] if parts else "Guest"


def format_phone(digits: str) -> str:
    raw = normalize_phone(digits)
    if len(raw) != 10:
        return digits or ""
    return f"({raw[:3]}) {raw[3:6]}-{raw[6:]}"


def format_clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def empty_state(day: str | None = None) -> dict:
    return {
        "day": day or business_date(),
        "display_enabled": False,
        "email_sent": False,
        "clients": [],
    }


class QueueStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        if not DATA_FILE.exists():
            self._write(empty_state())

    def _read(self) -> dict:
        with DATA_FILE.open(encoding="utf-8") as fh:
            return json.load(fh)

    def _write(self, state: dict) -> None:
        tmp = DATA_FILE.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        tmp.replace(DATA_FILE)

    def mutate(self, fn):
        with self._lock:
            state = self._read()
            rolled = self._maybe_rollover(state)
            result = fn(state)
            self._write(state)
            return result, rolled

    def snapshot(self) -> tuple[dict, dict | None]:
        with self._lock:
            state = self._read()
            rolled = self._maybe_rollover(state)
            if rolled:
                self._write(state)
            return json.loads(json.dumps(state)), rolled

    def _maybe_rollover(self, state: dict) -> dict | None:
        today = business_date()
        if state.get("day") == today:
            return None
        previous = json.loads(json.dumps(state))
        archive_path = ARCHIVE_DIR / f"{previous.get('day', 'unknown')}.json"
        archive_path.write_text(json.dumps(previous, indent=2), encoding="utf-8")
        state.clear()
        state.update(empty_state(today))
        return previous


def waiting_clients(state: dict) -> list[dict]:
    waiting = [c for c in state["clients"] if c["status"] == "waiting"]
    waiting.sort(key=lambda c: c["joined_at"])
    return waiting


def wait_minutes_ahead(waiting: list[dict], client_id: str) -> int:
    minutes = 0
    for person in waiting:
        if person["id"] == client_id:
            break
        minutes += int(person["session_minutes"]) + BUFFER_MINUTES
    return minutes


def estimate_for(state: dict, client: dict, at: datetime | None = None) -> dict:
    when = at or now()
    waiting = waiting_clients(state)
    if client["status"] != "waiting":
        return {
            "status": client["status"],
            "wait_minutes": None,
            "return_at": None,
            "return_label": None,
        }
    minutes = wait_minutes_ahead(waiting, client["id"])
    clock = datetime.fromtimestamp(when.timestamp() + minutes * 60, TZ)
    return {
        "status": "waiting",
        "wait_minutes": minutes,
        "return_at": clock.isoformat(),
        "return_label": format_clock(clock),
    }


def attach_estimates(state: dict, at: datetime | None = None) -> list[dict]:
    when = at or now()
    waiting = waiting_clients(state)
    enriched = []
    for client in state["clients"]:
        row = dict(client)
        if client["status"] == "waiting":
            minutes = wait_minutes_ahead(waiting, client["id"])
            clock = datetime.fromtimestamp(when.timestamp() + minutes * 60, TZ)
            row["wait_minutes"] = minutes
            row["return_at"] = clock.isoformat()
            row["return_label"] = format_clock(clock)
            row["first_name"] = first_name(client["name"])
        else:
            row["wait_minutes"] = None
            row["return_at"] = None
            row["return_label"] = None
            row["first_name"] = first_name(client["name"])
        enriched.append(row)
    return enriched


def add_client(state: dict, name: str, phone: str, session_minutes: int, payment: str) -> dict:
    client = {
        "id": uuid.uuid4().hex,
        "name": name.strip(),
        "phone": phone,
        "session_minutes": session_minutes,
        "payment": payment,
        "status": "waiting",
        "joined_at": now().isoformat(),
        "finished_at": None,
        "seen": False,
    }
    state["clients"].append(client)
    return client


def set_status(state: dict, client_id: str, status: str) -> dict | None:
    for client in state["clients"]:
        if client["id"] == client_id:
            client["status"] = status
            client["finished_at"] = now().isoformat()
            client["seen"] = status == "done"
            return client
    return None
