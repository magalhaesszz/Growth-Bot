import copy
import unittest
from types import SimpleNamespace

from instagram.stories import StoriesViewer


class FakeStateDB:
    def __init__(self):
        self.data = {}

    def get_json(self, key, default=None):
        return copy.deepcopy(self.data.get(key, default))

    def set_json(self, key, value):
        self.data[key] = copy.deepcopy(value)
        return True


class FakeRiskDetector:
    def __init__(self):
        self.paused = False
        self.successes = 0
        self.errors = []
        self.session_expired = 0

    def is_paused(self, username):
        return self.paused

    def record_success(self, username):
        self.successes += 1

    def record_error(self, username, error):
        self.errors.append(error)

    def notify_session_expired(self, username):
        self.session_expired += 1
        self.paused = True


class FakeApi:
    user_id = "999"

    def __init__(self):
        self.stories = {}
        self.seen_calls = []
        self.story_seen_result = True
        self.following = {}
        self.tray = {"tray": []}

    def user_stories(self, user_id):
        value = self.stories.get(str(user_id), [])
        if isinstance(value, Exception):
            raise value
        return value

    def story_seen(self, story_pks):
        self.seen_calls.append(list(story_pks))
        return self.story_seen_result

    def user_following(self, user_id, amount=0):
        return self.following

    def get_reels_tray_feed(self, reason="pull_to_refresh"):
        return self.tray


class FakeInstagramClient:
    username = "conta"

    def __init__(self, api=None):
        self.api = api or FakeApi()


class StoriesViewerTests(unittest.TestCase):
    @staticmethod
    def story(pk):
        return SimpleNamespace(pk=str(pk))

    def test_marks_all_active_stories_as_seen_once(self):
        api = FakeApi()
        api.stories["123"] = [self.story(10), self.story(11)]
        state = FakeStateDB()
        risk = FakeRiskDetector()
        viewer = StoriesViewer(FakeInstagramClient(api), risk, state_db=state)

        result = viewer.view_stories_for_users(
            ["123"], max_per_run=1, delay_min=0, delay_max=0
        )

        self.assertEqual(result["viewed"], 2)
        self.assertEqual(api.seen_calls, [[10, 11]])
        self.assertEqual(risk.successes, 1)
        self.assertIn("10", state.data["stories_seen:conta"])
        self.assertIn("11", state.data["stories_seen:conta"])

    def test_new_story_is_seen_but_old_story_is_not_repeated(self):
        api = FakeApi()
        state = FakeStateDB()
        risk = FakeRiskDetector()

        api.stories["123"] = [self.story(10), self.story(11)]
        first = StoriesViewer(FakeInstagramClient(api), risk, state_db=state)
        first.view_stories_for_users(
            ["123"], max_per_run=1, delay_min=0, delay_max=0
        )

        api.seen_calls.clear()
        api.stories["123"] = [self.story(10), self.story(11), self.story(12)]
        second = StoriesViewer(FakeInstagramClient(api), risk, state_db=state)
        result = second.view_stories_for_users(
            ["123"], max_per_run=1, delay_min=0, delay_max=0
        )

        self.assertEqual(result["viewed"], 1)
        self.assertEqual(result["already_seen"], 2)
        self.assertEqual(api.seen_calls, [[12]])
        self.assertIn("12", state.data["stories_seen:conta"])

    def test_story_seen_failure_is_not_persisted_as_viewed(self):
        api = FakeApi()
        api.story_seen_result = False
        api.stories["123"] = [self.story(20)]
        state = FakeStateDB()
        risk = FakeRiskDetector()
        viewer = StoriesViewer(FakeInstagramClient(api), risk, state_db=state)

        result = viewer.view_stories_for_users(
            ["123"], max_per_run=1, delay_min=0, delay_max=0
        )

        self.assertEqual(result["viewed"], 0)
        self.assertEqual(result["errors"], 1)
        self.assertNotIn("20", state.data.get("stories_seen:conta", {}))
        self.assertEqual(len(risk.errors), 1)

    def test_login_required_pauses_and_stops_cycle(self):
        api = FakeApi()
        api.stories["123"] = RuntimeError("login_required")
        api.stories["456"] = [self.story(30)]
        risk = FakeRiskDetector()
        viewer = StoriesViewer(
            FakeInstagramClient(api), risk, state_db=FakeStateDB()
        )

        result = viewer.view_stories_for_users(
            ["123", "456"], max_per_run=2, delay_min=0, delay_max=0
        )

        self.assertEqual(result["errors"], 1)
        self.assertEqual(risk.session_expired, 1)
        self.assertEqual(api.seen_calls, [])

    def test_following_ids_use_live_instagram_following_list(self):
        api = FakeApi()
        api.following = {
            123: SimpleNamespace(username="um"),
            456: SimpleNamespace(username="dois"),
        }
        viewer = StoriesViewer(
            FakeInstagramClient(api), FakeRiskDetector(), state_db=FakeStateDB()
        )

        self.assertEqual(viewer.get_following_user_ids(), ["123", "456"])

    def test_tray_filters_to_people_the_account_follows(self):
        api = FakeApi()
        api.tray = {
            "tray": [
                {
                    "id": "123",
                    "user": {"pk": "123"},
                    "latest_reel_media": 1000,
                },
                {
                    "id": "456",
                    "user": {"pk": "456"},
                    "latest_reel_media": 2000,
                },
                {
                    "id": "999",
                    "user": {"pk": "999"},
                    "latest_reel_media": 3000,
                },
            ]
        }
        viewer = StoriesViewer(
            FakeInstagramClient(api), FakeRiskDetector(), state_db=FakeStateDB()
        )

        markers = viewer.get_tray_user_markers({"123"})

        self.assertEqual(markers, {"123": "1000"})


if __name__ == "__main__":
    unittest.main()
