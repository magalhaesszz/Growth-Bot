import unittest

from instagram.score import ProfileScorer


class ProfileScorerTests(unittest.TestCase):
    def test_unknown_metrics_do_not_receive_ratio_or_post_points(self):
        profile = {
            "username": "perfil",
            "profile_pic_url": "https://example.test/p.jpg",
            "full_name": "Pessoa Real",
            "is_private": False,
            "follower_count": None,
            "following_count": None,
            "media_count": None,
        }
        self.assertEqual(ProfileScorer().score(profile), 50)

    def test_known_healthy_metrics_can_score_full_points(self):
        profile = {
            "username": "perfil",
            "profile_pic_url": "https://example.test/p.jpg",
            "full_name": "Pessoa Real",
            "is_private": False,
            "follower_count": 1000,
            "following_count": 500,
            "media_count": 10,
        }
        self.assertEqual(ProfileScorer().score(profile), 100)


if __name__ == "__main__":
    unittest.main()
