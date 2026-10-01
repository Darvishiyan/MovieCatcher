import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import subtitles


class SubtitleTests(unittest.TestCase):
    def test_rejects_wrong_episode_and_wrong_release_source(self):
        video = dict(subtitles.guessit("Silo.S02E07.2160p.WEB-DL.HDR.x265.mkv"))
        self.assertLess(subtitles._release_score(video, "Silo.S02E08.2160p.WEB-DL", {"season": 2, "episode": 8}), 0)
        self.assertLess(subtitles._release_score(video, "Silo.S02E07.2160p.BluRay", {"season": 2, "episode": 7}), 0)
        self.assertGreaterEqual(subtitles._release_score(video, "Silo.S02E07.2160p.WEB-DL", {"season": 2, "episode": 7}), 75)

    def test_srt_duration_must_fit_video(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "film.en.srt"
            path.write_text("\n\n".join(
                f"{number}\n00:{number:02}:00,000 --> 00:{number:02}:02,000\nLine {number}"
                for number in range(1, 11)
            ), encoding="utf-8")
            self.assertTrue(subtitles._valid_srt(path, 12 * 60))
            self.assertFalse(subtitles._valid_srt(path, 120 * 60))

    def test_existing_sidecar_skips_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "Silo.S02E07.mkv"
            video.write_bytes(b"video")
            video.with_name("Silo.S02E07.en.srt").write_text("existing")
            with patch.object(subtitles, "_probe") as probe:
                self.assertEqual(subtitles.fetch_english_subtitle(video), "external")
                probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
