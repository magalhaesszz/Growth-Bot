import os
import unittest
from unittest.mock import patch

os.environ.setdefault("VIDEO_API_URL", "https://video.example.test")
os.environ.setdefault("VIDEO_API_SECRET", "test-secret")
os.environ.setdefault("VIDEO_MAX_FILE_MB", "45")
os.environ.setdefault("VIDEO_MAX_BATCH_MB", "120")

import httpx

import video_client


class VideoClientTests(unittest.TestCase):
    def test_html_error_response_is_handled(self):
        response = httpx.Response(500, text="internal server error")
        self.assertEqual(video_client._response_error(response), "internal server error")

    def test_missing_configuration_returns_error_instead_of_raising(self):
        with patch.object(video_client, "VIDEO_API_URL", ""), patch.object(
            video_client, "VIDEO_API_SECRET", ""
        ):
            result = video_client.api_status()
        self.assertFalse(result["ok"])
        self.assertIn("VIDEO_API_URL", result["error"])

    def test_oversized_video_is_rejected_before_network_request(self):
        with patch.object(video_client, "VIDEO_MAX_FILE_MB", 1):
            with self.assertRaises(ValueError):
                video_client._validate_video_size(b"x" * (1024 * 1024 + 1), "big.mp4")

    def test_batch_total_limit_is_enforced(self):
        videos = [(b"x" * 700_000, "a.mp4"), (b"y" * 700_000, "b.mp4")]
        with patch.object(video_client, "VIDEO_MAX_FILE_MB", 2), patch.object(
            video_client, "VIDEO_MAX_BATCH_MB", 1
        ):
            with self.assertRaises(ValueError):
                video_client._validate_batch_size(videos)


if __name__ == "__main__":
    unittest.main()
