import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from browser_use_rs.agent import _task_message_with_runtime_context  # noqa: E402


class RuntimeContextTests(unittest.TestCase):
    def test_task_message_includes_current_date_context(self):
        now = datetime(
            2026,
            5,
            14,
            12,
            30,
            tzinfo=timezone(timedelta(hours=-7), "PDT"),
        )
        task = (
            "Use the search bar to find events for the upcoming weekend. "
            "website: https://eventbrite.com"
        )

        message = _task_message_with_runtime_context(task, now=now)

        self.assertIn("<runtime_context>", message)
        self.assertIn("Current date: Thursday, 2026-05-14 (PDT)", message)
        self.assertIn('"upcoming weekend"', message)
        self.assertIn("<user_request>", message)
        self.assertIn(task, message)


if __name__ == "__main__":
    unittest.main()
