import os
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-key")

from database.operations import _local_day_bounds


class OperationTimeTests(unittest.TestCase):
    def test_daily_bounds_follow_sao_paulo_midnight(self):
        sao_paulo = ZoneInfo("America/Sao_Paulo")
        now = datetime(2026, 8, 18, 23, 30, tzinfo=sao_paulo)
        start, end = _local_day_bounds(now)
        self.assertTrue(start.startswith("2026-08-18T03:00:00"))
        self.assertTrue(end.startswith("2026-08-19T03:00:00"))


if __name__ == "__main__":
    unittest.main()
