import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dolby_vision


def video_info(profile=8):
    return {
        "streams": [
            {
                "codec_type": "video", "codec_name": "hevc", "width": 3840,
                "height": 1606, "color_transfer": "smpte2084",
                "color_primaries": "bt2020", "color_space": "bt2020nc",
                "side_data_list": [{
                    "side_data_type": "DOVI configuration record",
                    "rpu_present_flag": 1, "dv_profile": profile,
                    "bl_present_flag": 1,
                }],
            },
            {"codec_type": "audio", "codec_name": "aac", "channels": 2,
             "tags": {"language": "per"}},
        ],
        "format": {"duration": "100.0"},
    }


class DolbyVisionTests(unittest.TestCase):
    def test_non_dolby_file_is_left_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "movie.mkv"
            source.write_bytes(b"original")
            info = video_info()
            info["streams"][0]["side_data_list"] = []
            with patch.object(dolby_vision, "probe", return_value=info):
                self.assertFalse(dolby_vision.remove_dolby_vision(source))
            self.assertEqual(source.read_bytes(), b"original")

    def test_unsupported_profile_never_replaces_original(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "movie.mkv"
            source.write_bytes(b"original")
            with patch.object(dolby_vision, "probe", return_value=video_info(5)):
                with self.assertRaises(dolby_vision.DolbyVisionError):
                    dolby_vision.remove_dolby_vision(source)
            self.assertEqual(source.read_bytes(), b"original")

    def test_verification_rejects_lost_audio(self):
        source = video_info()
        result = video_info()
        result["streams"][0]["side_data_list"] = []
        result["streams"].pop()
        with self.assertRaisesRegex(dolby_vision.DolbyVisionError, "number of media streams"):
            dolby_vision._verify(source, result)
