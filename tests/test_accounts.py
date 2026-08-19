import os
import unittest

os.environ.setdefault(
    "SESSION_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
)
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-key")

from database.accounts import AccountsDB, _encrypt, _validate_combined_settings


class FakeResponse:
    def __init__(self, data=None):
        self.data = data or []


class FakeTable:
    def __init__(self, row):
        self.row = row
        self.pending_update = None

    def select(self, *args, **kwargs):
        return self

    def eq(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def update(self, payload):
        self.pending_update = dict(payload)
        return self

    def insert(self, payload):
        raise AssertionError("existing account must not be inserted")

    def execute(self):
        if self.pending_update is not None:
            self.row.update(self.pending_update)
            self.pending_update = None
        return FakeResponse([dict(self.row)])


class FakeSupabase:
    def __init__(self, row):
        self.ig_accounts = FakeTable(row)

    def table(self, name):
        if name != "ig_accounts":
            raise AssertionError(name)
        return self.ig_accounts


class AccountsTests(unittest.TestCase):
    def test_empty_sessionid_password_does_not_overwrite_existing_password(self):
        original_token = _encrypt("senha-original")
        row = {
            "id": "account-1",
            "username": "conta",
            "password_enc": original_token,
            "fingerprint": {"device": "x"},
            "status": "active",
            "warmup_day": 0,
        }
        db = object.__new__(AccountsDB)
        db.sb = FakeSupabase(row)

        result = db.add_account("conta", "", fingerprint=None)
        self.assertEqual(result["password"], "senha-original")
        self.assertEqual(row["password_enc"], original_token)

    def test_settings_reject_inverted_delay(self):
        settings = {
            "daily_follows": 40,
            "daily_unfollows": 40,
            "hour_start": 8,
            "hour_end": 22,
            "delay_min": 120,
            "delay_max": 30,
            "score_min": 50,
            "unfollow_after_days": 5,
            "unfollow_policy": "keep_follow_backs",
            "daily_report_enabled": True,
        }
        with self.assertRaises(ValueError):
            _validate_combined_settings(settings)

    def test_settings_reject_invalid_hours(self):
        settings = {
            "daily_follows": 40,
            "daily_unfollows": 40,
            "hour_start": 22,
            "hour_end": 8,
            "delay_min": 30,
            "delay_max": 90,
            "score_min": 50,
            "unfollow_after_days": 5,
            "unfollow_policy": "keep_follow_backs",
            "daily_report_enabled": True,
        }
        with self.assertRaises(ValueError):
            _validate_combined_settings(settings)


if __name__ == "__main__":
    unittest.main()
