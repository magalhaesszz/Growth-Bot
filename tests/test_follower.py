import threading
import unittest
from unittest.mock import patch

from instagram.follower import Follower


class FakeApi:
    def __init__(self):
        self.followed = []

    def user_follow(self, user_id):
        self.followed.append(user_id)


class FakeClient:
    username = "conta"

    def __init__(self):
        self.api = FakeApi()

    def save_session(self):
        pass


class FakeRisk:
    def is_paused(self, username):
        return False

    def record_success(self, username):
        pass

    def record_error(self, username, error):
        pass

    def notify_session_expired(self, username):
        pass


class PassScorer:
    def passes(self, profile, min_score):
        return True


class PassBlacklist:
    def is_blocked(self, profile):
        return False


class FollowerTests(unittest.TestCase):
    @patch("instagram.follower._sleep", return_value=True)
    def test_limit_is_shared_across_multiple_target_batches(self, _sleep):
        client = FakeClient()
        follower = Follower(client, FakeRisk(), PassScorer(), PassBlacklist())
        first = follower.follow_batch(
            [{"username": "a", "user_id": "1"}],
            daily_limit=2,
            min_score=0,
            delay_min=0,
            delay_max=0,
        )
        second = follower.follow_batch(
            [
                {"username": "b", "user_id": "2"},
                {"username": "c", "user_id": "3"},
            ],
            daily_limit=2,
            min_score=0,
            delay_min=0,
            delay_max=0,
        )
        self.assertEqual(first["followed"], 1)
        self.assertEqual(second["followed"], 1)
        self.assertEqual(client.api.followed, [1, 2])

    def test_pre_set_stop_event_prevents_first_action(self):
        client = FakeClient()
        follower = Follower(client, FakeRisk(), PassScorer(), PassBlacklist())
        stop = threading.Event()
        stop.set()
        result = follower.follow_batch(
            [{"username": "a", "user_id": "1"}],
            daily_limit=10,
            min_score=0,
            delay_min=0,
            delay_max=0,
            stop_event=stop,
        )
        self.assertTrue(result["stopped"])
        self.assertEqual(client.api.followed, [])


if __name__ == "__main__":
    unittest.main()
