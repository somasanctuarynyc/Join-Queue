import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from store import SESSION_OPTIONS, attach_estimates, wait_minutes_ahead, waiting_clients


TZ = ZoneInfo("America/New_York")


def client(cid, minutes, joined):
    return {
        "id": cid,
        "name": cid,
        "phone": "5550000000",
        "session_minutes": minutes,
        "payment": "Cash",
        "status": "waiting",
        "joined_at": joined,
        "seen": False,
    }


class SessionOptionsTests(unittest.TestCase):
    def test_session_lengths(self):
        self.assertEqual(SESSION_OPTIONS, (5, 10, 15, 20))


class WaitMathTests(unittest.TestCase):
    def test_buffer_per_person_ahead(self):
        state = {
            "clients": [
                client("a", 15, "2026-09-26T12:00:00-04:00"),
                client("b", 10, "2026-09-26T12:01:00-04:00"),
                client("c", 20, "2026-09-26T12:02:00-04:00"),
            ]
        }
        waiting = waiting_clients(state)
        self.assertEqual(wait_minutes_ahead(waiting, "a"), 0)
        self.assertEqual(wait_minutes_ahead(waiting, "b"), 20)
        self.assertEqual(wait_minutes_ahead(waiting, "c"), 20 + 15)

    def test_done_drops_out_of_estimate(self):
        state = {
            "clients": [
                {**client("a", 15, "2026-09-26T12:00:00-04:00"), "status": "done", "seen": True},
                client("b", 10, "2026-09-26T12:01:00-04:00"),
            ]
        }
        at = datetime(2026, 9, 26, 12, 10, tzinfo=TZ)
        rows = {row["id"]: row for row in attach_estimates(state, at)}
        self.assertEqual(rows["b"]["wait_minutes"], 0)
        self.assertEqual(rows["b"]["return_label"], "12:10 PM")


if __name__ == "__main__":
    unittest.main()
