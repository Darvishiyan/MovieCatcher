import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import subtitle_normalize as normalizer


class SubtitleNormalizationTests(unittest.TestCase):
    def test_sidecar_belongs_to_longest_matching_video_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            standard = root / "Film.2024.mkv"
            extended = root / "Film.2024.Extended.mkv"
            standard.touch()
            extended.touch()
            short_sidecar = root / "Film.2024.en.srt"
            long_sidecar = root / "Film.2024.Extended.en.srt"
            short_sidecar.touch()
            long_sidecar.touch()
            self.assertEqual(normalizer.associated_sidecars(standard), [short_sidecar])
            self.assertEqual(normalizer.associated_sidecars(extended), [long_sidecar])

    def test_prefers_explicit_us_english_over_sdh(self):
        american = {"index": 4, "tags": {"language": "en-US", "title": "English (US)"}}
        sdh = {"index": 3, "tags": {"language": "eng", "title": "English SDH"}}
        self.assertGreater(normalizer._score_stream(american), normalizer._score_stream(sdh))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
    def test_mp4_with_subrip_persian_remuxes_to_mov_text_without_video_reencoding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "Cartoon.2024.mp4"
            subtitle = root / "persian.srt"
            subtitle.write_text("\n\n".join(
                f"{number + 1}\n00:00:{number:02},000 --> 00:00:{number:02},500\n"
                "این زیرنویس فارسی است و پژواک خوبی دارد"
                for number in range(10)
            ), encoding="utf-8")
            # Reproduce MP4-named files carrying Matroska-style SubRip streams.
            subprocess.run([
                "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                "color=c=black:s=64x64:r=2:d=12", "-i", str(subtitle),
                "-map", "0:v", "-map", "1:s", "-c:v", "mpeg4", "-c:s", "srt",
                "-metadata:s:s:0", "language=und", "-f", "matroska", str(video),
            ], check=True, capture_output=True)
            before = normalizer.probe(video)
            self.assertEqual(normalizer.normalize_english_subtitles(video), "normalized")
            after = normalizer.probe(video)
            self.assertEqual([stream["codec_name"] for stream in before["streams"] if stream["codec_type"] == "video"],
                             [stream["codec_name"] for stream in after["streams"] if stream["codec_type"] == "video"])
            subtitles = [stream for stream in after["streams"] if stream["codec_type"] == "subtitle"]
            self.assertEqual(len(subtitles), 1)
            self.assertEqual(subtitles[0]["codec_name"], "mov_text")
            self.assertEqual(subtitles[0].get("tags", {}).get("language"), "per")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
    def test_remux_keeps_persian_audio_video_and_one_external_english(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "base.mkv"
            video = root / "Film.2024.mkv"
            subprocess.run([
                "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:r=2:d=12",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=12", "-c:v", "mpeg4", "-c:a", "aac", str(base),
            ], check=True, capture_output=True)
            subtitles = []
            for name, text in (("first", "English line"), ("second", "Another English line"), ("persian", "سلام فارسی"), ("spanish", "Hola mundo"), ("unlabeled_persian", "این نوشته برای فیلم فارسی است و گوینده با چهرهٔ خندان می‌گوید")):
                path = root / f"{name}.srt"
                path.write_text("\n\n".join(
                    f"{number + 1}\n00:00:{number:02},000 --> 00:00:{number:02},500\n{text} {number}"
                    for number in range(10)
                ), encoding="utf-8")
                subtitles.append(path)
            subprocess.run([
                "ffmpeg", "-nostdin", "-v", "error", "-i", str(base),
                *[part for subtitle in subtitles for part in ("-i", str(subtitle))],
                "-map", "0", "-map", "1:0", "-map", "2:0", "-map", "3:0", "-map", "4:0", "-map", "5:0",
                "-c", "copy", "-c:s", "srt",
                "-metadata:s:s:0", "language=eng", "-metadata:s:s:1", "language=eng",
                "-metadata:s:s:2", "language=per", "-metadata:s:s:3", "language=spa",
                "-metadata:s:s:4", "language=und", str(video),
            ], check=True, capture_output=True)
            sidecar = root / "Film.2024.en.srt"
            shutil.copyfile(subtitles[0], sidecar)
            persian_sidecar = root / "Film.2024.fa.srt"
            shutil.copyfile(subtitles[2], persian_sidecar)
            before = normalizer.probe(video)
            self.assertEqual(len(normalizer.english_streams(before)), 2)
            self.assertEqual(normalizer.normalize_english_subtitles(video), "normalized")
            after = normalizer.probe(video)
            self.assertEqual(len(normalizer.english_streams(after)), 0)
            self.assertEqual(sum(stream.get("tags", {}).get("language") == "per" for stream in after["streams"]), 2)
            self.assertEqual(sum(stream.get("tags", {}).get("language") == "spa" for stream in after["streams"]), 0)
            self.assertEqual(sum(stream.get("codec_type") == "video" for stream in after["streams"]), 1)
            self.assertEqual(sum(stream.get("codec_type") == "audio" for stream in after["streams"]), 1)
            self.assertTrue(sidecar.exists())
            self.assertTrue(persian_sidecar.exists())
            self.assertEqual(len(normalizer.english_sidecars(video)), 1)
            self.assertFalse(list(root.glob(".moviecatcher-subclear-*")))


if __name__ == "__main__":
    unittest.main()
