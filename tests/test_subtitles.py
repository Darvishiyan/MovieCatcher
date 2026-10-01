import io
import json
import logging
import os
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from types import SimpleNamespace
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
            self.assertFalse(subtitles._valid_srt(path, 14 * 60))
            self.assertFalse(subtitles._valid_srt(path, 120 * 60))

    def test_sorts_a_few_misplaced_srt_cues_without_changing_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "film.en.srt"
            blocks = [
                f"{number + 1}\n00:{number:02}:00,000 --> 00:{number:02}:02,000\nLine {number}"
                for number in range(12)
            ]
            blocks[3], blocks[7] = blocks[7], blocks[3]
            path.write_text("\n\n".join(blocks), encoding="utf-8")
            self.assertFalse(subtitles._valid_srt(path, 12 * 60))
            self.assertTrue(subtitles._repair_srt_order(path, 12 * 60))
            self.assertTrue(subtitles._valid_srt(path, 12 * 60))
            self.assertIn("Line 3", path.read_text(encoding="utf-8"))

    def test_public_provider_retries_an_incomplete_release(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "Film.2024.1080p.WEB-DL.mkv"
            candidate = Path(directory) / "Film.2024.1080p.WEB-DL.en.srt"
            calls = []

            def download(command, **_):
                calls.append(command)
                interval = 30 if len(calls) == 1 else 70
                candidate.write_text("\n\n".join(
                    f"{number + 1}\n00:{number * interval // 60:02}:{number * interval % 60:02},000 --> "
                    f"00:{number * interval // 60:02}:{number * interval % 60:02},500\nEnglish line {number}"
                    for number in range(10)
                ), encoding="utf-8")
                subtitle_id = "first" if len(calls) == 1 else "second"
                return SimpleNamespace(
                    returncode=0, stdout="",
                    stderr=(f"Saving <OpenSubtitlesSubtitle '{subtitle_id}' [en]>\n"
                            "English subtitle from opensubtitles (match on title, year, source)"),
                )

            with patch.object(subtitles.subprocess, "run", side_effect=download):
                self.assertTrue(subtitles._fetch_other_providers(video, candidate, 720))
            self.assertEqual(len(calls), 2)
            self.assertIn("first", calls[1])
            self.assertTrue(subtitles._valid_srt(candidate, 720))

    def test_existing_sidecar_skips_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "Silo.S02E07.mkv"
            video.write_bytes(b"video")
            video.with_name("Silo.S02E07.en.srt").write_text("existing")
            with patch.object(subtitles, "_probe") as probe:
                self.assertEqual(subtitles.fetch_english_subtitle(video), "external")
                probe.assert_not_called()

    def test_subdl_tries_matching_release_and_validates_archive(self):
        captions = "\n\n".join(
            f"{number}\n00:{number:02}:00,000 --> 00:{number:02}:02,000\nLine {number}"
            for number in range(1, 11)
        )
        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(archive_buffer, "w") as archive:
            archive.writestr("Silo.S02E07.en.srt", captions)
        response = {
            "status": True,
            "results": [{"name": "Silo", "type": "tv"}],
            "subtitles": [{
                "release_name": "Silo.S02E07.2160p.WEB-DL",
                "season": 2, "episode": 7, "language": "EN",
                "name": "Silo.zip", "url": "/subtitle/test.zip",
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "Silo.S02E07.en.srt"
            responses = [io.BytesIO(json.dumps(response).encode()), io.BytesIO(archive_buffer.getvalue())]
            with patch.object(subtitles.urllib.request, "urlopen", side_effect=responses) as request:
                self.assertTrue(subtitles._fetch_subdl(
                    Path(directory) / "Silo.S02E07.2160p.WEB-DL.mkv", target, 720, "example-key",
                ))
            self.assertEqual(request.call_count, 2)
            self.assertIn("Line 10", target.read_text(encoding="utf-8"))

    def test_movie_without_subdl_key_tries_public_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "Interstellar.2014.1080p.BluRay.mkv"
            video.write_bytes(b"video")
            with patch.dict(subtitles.os.environ, {"SUBDL_API_KEY": ""}):
                with patch.object(subtitles, "_probe", return_value={"streams": [], "format": {"duration": "7200"}}):
                    with patch.object(subtitles, "_fetch_other_providers", return_value=False) as fetch:
                        self.assertEqual(subtitles.fetch_english_subtitle(video), "unavailable")
                        fetch.assert_called_once()

    def test_public_provider_saves_one_valid_english_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "Southpaw.2015.1080p.BluRay.x264.YIFY.mp4"
            video.write_bytes(b"video")

            def supply_subtitle(_, candidate, __):
                candidate.write_text("\n\n".join(
                    f"{number}\n00:{number:02}:00,000 --> 00:{number:02}:02,000\nEnglish line {number}"
                    for number in range(1, 11)
                ), encoding="utf-8")
                return True

            with patch.dict(subtitles.os.environ, {"SUBDL_API_KEY": ""}):
                with patch.object(subtitles, "_probe", return_value={"streams": [], "format": {"duration": "720"}}):
                    with patch.object(subtitles, "_fetch_other_providers", side_effect=supply_subtitle):
                        self.assertEqual(subtitles.fetch_english_subtitle(video), "downloaded")
            self.assertTrue(video.with_name("Southpaw.2015.1080p.BluRay.x264.YIFY.en.srt").exists())

    def test_existing_embedded_english_does_not_create_duplicate_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "Interstellar.2014.1080p.BluRay.mkv"
            video.write_bytes(b"video")

            with patch.dict(subtitles.os.environ, {"SUBDL_API_KEY": "example-key"}):
                with patch.object(subtitles, "_probe", return_value={
                    "streams": [{"codec_type": "subtitle", "tags": {"language": "eng"}}],
                    "format": {"duration": "720"},
                }):
                    with patch.object(subtitles, "_fetch_subdl") as fetch:
                        self.assertEqual(subtitles.fetch_english_subtitle(video), "embedded")
                        fetch.assert_not_called()
            self.assertFalse(video.with_name("Interstellar.2014.1080p.BluRay.en.srt").exists())

    def test_subdl_429_is_reported_as_pending_without_leaking_key(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "Interstellar.2014.1080p.BluRay.mkv"
            video.write_bytes(b"video")
            error = urllib.error.HTTPError("https://api.subdl.com/?api_key=secret", 429, "quota", {"Retry-After": "60"}, None)
            with patch.dict(subtitles.os.environ, {"SUBDL_API_KEY": "secret"}):
                with patch.object(subtitles, "_probe", return_value={"format": {"duration": "7200"}}):
                    with patch.object(subtitles.urllib.request, "urlopen", side_effect=error):
                        with patch.object(subtitles.logger, "warning") as warning:
                            with patch.object(subtitles.time, "time", return_value=1000):
                                with patch.object(subtitles, "_next_subdl_download_at", 0):
                                    with patch.object(subtitles, "_fetch_other_providers", return_value=False) as fallback:
                                        self.assertEqual(subtitles.fetch_english_subtitle(video), "rate_limited")
                                        fallback.assert_called_once()
            self.assertNotIn("secret", str(warning.call_args))

    def test_error_log_is_private_and_persistent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = logging.getLogger()
            try:
                path = subtitles.configure_error_log(Path(directory))
                root.warning("persistent test error")
                self.assertIn("persistent test error", path.read_text(encoding="utf-8"))
                if os.name != "nt":
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            finally:
                for handler in list(root.handlers):
                    if isinstance(handler, subtitles._PrivateRotatingFileHandler) and handler.baseFilename == str(path):
                        root.removeHandler(handler)
                        handler.close()


if __name__ == "__main__":
    unittest.main()
