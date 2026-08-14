import unittest
from types import SimpleNamespace
from unittest.mock import patch

from instagram.unfollower import Unfollower


class FakeApi:
    def __init__(self, follows_back=False):
        self.follows_back = follows_back
        self.unfollowed = []

    def user_friendship(self, user_id):
        return SimpleNamespace(followed_by=self.follows_back)

    def user_unfollow(self, user_id):
        self.unfollowed.append(user_id)


class FakeInstagramClient:
    username = "conta"

    def __init__(self, follows_back=False):
        self.api = FakeApi(follows_back)

    def save_session(self):
        pass


class FakeRiskDetector:
    def is_paused(self, username):
        return False

    def record_success(self, username):
        pass

    def record_error(self, username, error):
        pass


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


if __name__ == "__main__":
    unittest.main()
