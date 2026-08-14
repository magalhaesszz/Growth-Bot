import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import video_settings


class VideoSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(
            video_settings,
            "_PATH",
            Path(self.temp.name) / "video-settings.json",
        )
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.temp.cleanup()

    def test_values_are_validated_and_persisted(self):
        saved = video_settings.set_values(
            10,
            {"video_width": "900", "position_x": "0.7", "antiban": "false"},
        )
        self.assertEqual(saved["video_width"], 900)
        self.assertEqual(saved["position_x"], 0.7)
        self.assertFalse(saved["antiban"])
        self.assertEqual(video_settings.get_config(10), saved)

    def test_unknown_and_out_of_range_values_are_rejected(self):
        with self.assertRaises(ValueError):
            video_settings.set_values(10, {"unknown": "1"})
        with self.assertRaises(ValueError):
            video_settings.set_values(10, {"position_y": "2"})

    def test_reset_restores_defaults(self):
        video_settings.set_values(10, {"video_width": 500})
        self.assertEqual(video_settings.reset(10), video_settings.DEFAULTS)
        self.assertEqual(video_settings.get_config(10), video_settings.DEFAULTS)


if __name__ == "__main__":
    unittest.main()
