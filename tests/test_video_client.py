import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("VIDEO_API_URL", "https://video.example.test")
os.environ.setdefault("VIDEO_API_SECRET", "test-secret")
os.environ.setdefault("VIDEO_MAX_FILE_MB", "45")
os.environ.setdefault("VIDEO_MAX_BATCH_MB", "120")

import httpx

import video_client


class _StreamContext:
    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self.response

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeClient:
    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def stream(self, *args, **kwargs):
        return _StreamContext(self.response)


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

    def test_processed_video_can_be_streamed_to_temp_file(self):
        response = httpx.Response(
            200,
            content=b"processed-video-bytes",
            headers={"content-disposition": 'attachment; filename="editado_original.mp4"'},
        )
        fake_client = _FakeClient(response)

        with patch.object(video_client.httpx, "Client", return_value=fake_client):
            result = video_client.processar_video_arquivo(
                b"input", "original.mp4", "123", {"video_width": 750}
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["filename"], "editado_original.mp4")
        path = Path(result["video_path"])
        try:
            self.assertTrue(path.exists())
            self.assertEqual(path.read_bytes(), b"processed-video-bytes")
        finally:
            path.unlink(missing_ok=True)

    def test_streamed_output_limit_fails_without_returning_a_file(self):
        response = httpx.Response(200, content=b"x" * 2048)
        fake_client = _FakeClient(response)

        with patch.object(video_client.httpx, "Client", return_value=fake_client), patch.object(
            video_client, "VIDEO_MAX_FILE_MB", 0.001
        ):
            result = video_client.processar_video_arquivo(
                b"input", "original.mp4", "123", {}
            )

        self.assertFalse(result["ok"])
        self.assertNotIn("video_path", result)
        self.assertIn("limite seguro", result["error"])


if __name__ == "__main__":
    unittest.main()
