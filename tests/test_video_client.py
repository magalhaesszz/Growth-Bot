import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
