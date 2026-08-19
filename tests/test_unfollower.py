import unittest
from types import SimpleNamespace
from unittest.mock import patch

from instagram.score import WhitelistFilter
from instagram.unfollower import Unfollower


class FakeApi:
    def __init__(self, follows_back=False, friendship_error=False):
        self.follows_back = follows_back
        self.friendship_error = friendship_error
        self.unfollowed = []

    def user_friendship(self, user_id):
        if self.friendship_error:
            raise RuntimeError("network timeout")
        return SimpleNamespace(followed_by=self.follows_back)

    def user_unfollow(self, user_id):
        self.unfollowed.append(user_id)


class FakeInstagramClient:
    username = "conta"

    def __init__(self, follows_back=False, friendship_error=False):
        self.api = FakeApi(follows_back, friendship_error)

    def save_session(self):
        pass


class FakeRiskDetector:
    def is_paused(self, username):
        return False

    def record_success(self, username):
        pass

    def record_error(self, username, error):
        pass

    def notify_session_expired(self, username):
        pass


class FakeDB:
    def __init__(self):
        self.unfollowed = []
        self.follow_backs = []
        self.logs = []

    def get_following_list(self, account_id, limit=500):
        return [
            {
                "target_username": "perfil",
                "target_user_id": "123",
                "follows_back": False,
            }
        ]

    def mark_unfollowed(self, account_id, username):
        self.unfollowed.append(username)

    def mark_follows_back(self, account_id, username):
        self.follow_backs.append(username)

    def log_action(self, *args, **kwargs):
        self.logs.append((args, kwargs))


class UnfollowerTests(unittest.TestCase):
    candidate = {
        "id": "row-1",
        "target_username": "perfil",
        "target_user_id": "123",
    }

    @patch("instagram.unfollower.time.sleep", return_value=None)
    def test_uses_database_target_field_names(self, _sleep):
        client = FakeInstagramClient(follows_back=False)
        result = Unfollower(client, FakeRiskDetector()).unfollow_batch(
            [self.candidate], 1, 0, 0
        )
        self.assertEqual(result["unfollowed"], 1)
        self.assertEqual(client.api.unfollowed, [123])

    @patch("instagram.unfollower.time.sleep", return_value=None)
    def test_keep_follow_backs_policy_keeps_follower(self, _sleep):
        client = FakeInstagramClient(follows_back=True)
        result = Unfollower(client, FakeRiskDetector()).unfollow_batch(
            [self.candidate], 1, 0, 0, policy="keep_follow_backs"
        )
        self.assertEqual(result["kept"], 1)
        self.assertEqual(client.api.unfollowed, [])

    @patch("instagram.unfollower.time.sleep", return_value=None)
    def test_remove_all_policy_does_not_keep_follower(self, _sleep):
        client = FakeInstagramClient(follows_back=True)
        result = Unfollower(client, FakeRiskDetector()).unfollow_batch(
            [self.candidate], 1, 0, 0, policy="remove_all"
        )
        self.assertEqual(result["unfollowed"], 1)

    @patch("instagram.unfollower.time.sleep", return_value=None)
    def test_remove_only_follow_backs_keeps_non_follower(self, _sleep):
        client = FakeInstagramClient(follows_back=False)
        result = Unfollower(client, FakeRiskDetector()).unfollow_batch(
            [self.candidate], 1, 0, 0, policy="remove_only_follow_backs"
        )
        self.assertEqual(result["kept"], 1)
        self.assertEqual(client.api.unfollowed, [])

    @patch("instagram.unfollower.time.sleep", return_value=None)
    def test_friendship_error_never_authorizes_unfollow(self, _sleep):
        client = FakeInstagramClient(friendship_error=True)
        result = Unfollower(client, FakeRiskDetector()).unfollow_batch(
            [self.candidate], 1, 0, 0, policy="remove_all"
        )
        self.assertEqual(result["unfollowed"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(client.api.unfollowed, [])

    @patch("instagram.unfollower.time.sleep", return_value=None)
    def test_auto_unfollow_respects_whitelist(self, _sleep):
        client = FakeInstagramClient(follows_back=True)
        db = FakeDB()
        unfollower = Unfollower(
            client,
            FakeRiskDetector(),
            WhitelistFilter(["perfil"]),
        )
        count = unfollower.auto_unfollow_follow_backs(
            "account-1", db, daily_limit=5, delay_min=0, delay_max=0
        )
        self.assertEqual(count, 0)
        self.assertEqual(client.api.unfollowed, [])
        self.assertEqual(db.unfollowed, [])

    @patch("instagram.unfollower.time.sleep", return_value=None)
    def test_auto_unfollow_requires_confirmed_follow_back(self, _sleep):
        client = FakeInstagramClient(friendship_error=True)
        db = FakeDB()
        unfollower = Unfollower(client, FakeRiskDetector())
        count = unfollower.auto_unfollow_follow_backs(
            "account-1", db, daily_limit=5, delay_min=0, delay_max=0
        )
        self.assertEqual(count, 0)
        self.assertEqual(client.api.unfollowed, [])


if __name__ == "__main__":
    unittest.main()
