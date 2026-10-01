"""Keep one English subtitle per video while preserving Persian and all media streams."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path

from dolby_vision import VIDEO_SUFFIXES

logger = logging.getLogger(__name__)
ENGLISH_TAGS = {"en", "eng", "english", "en-us", "en_us"}
PERSIAN_TAGS = {"fa", "fas", "per", "farsi", "persian"}
SUBTITLE_SUFFIXES = {".srt", ".ass", ".ssa", ".vtt", ".sub"}
US_PATTERN = re.compile(r"(?i)\b(?:american|english\s*\(?us\)?|en[-_]us|united states)\b")
SDH_PATTERN = re.compile(r"(?i)\b(?:sdh|hearing.impaired|closed.caption|cc)\b")
FORCED_PATTERN = re.compile(r"(?i)\b(?:forced|foreign)\b")


class SubtitleNormalizationError(RuntimeError):
    """No source was replaced because an English subtitle could not be safely reconciled."""


def probe(video: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_chapters", "-show_format", "-of", "json", str(video)],
        capture_output=True, text=True, timeout=60, check=True,
    )
    return json.loads(result.stdout)


def english_streams(info: dict) -> list[dict]:
    return [
        stream for stream in info.get("streams", [])
        if stream.get("codec_type") == "subtitle"
        and str(stream.get("tags", {}).get("language", "")).lower() in ENGLISH_TAGS
    ]


def english_sidecars(video: Path) -> list[Path]:
    return [path for path in associated_sidecars(video) if _sidecar_language(video, path) == "english"]


def associated_sidecars(video: Path) -> list[Path]:
    prefix = video.stem + "."
    found = []
    siblings = list(video.parent.iterdir())
    longer_video_stems = [
        path.stem for path in siblings
        if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES
        and path.stem.startswith(prefix)
    ]
    for path in siblings:
        if not path.is_file() or path.suffix.lower() not in SUBTITLE_SUFFIXES or not path.name.startswith(prefix):
            continue
        if any(path.name.startswith(stem + ".") for stem in longer_video_stems):
            continue
        found.append(path)
    return found


def _sidecar_language(video: Path, path: Path) -> str:
    suffix = path.name[len(video.stem) + 1:-len(path.suffix)].lower()
    tokens = re.split(r"[._\s-]+", suffix)
    if any(token in {"en", "eng", "english"} for token in tokens):
        return "english"
    if any(token in PERSIAN_TAGS for token in tokens):
        return "persian"
    if tokens == [""] or all(token in {"default", "forced", "foreign", "sdh", "cc", "hi"} for token in tokens):
        return "unknown"
    return "other"


def _score_stream(stream: dict) -> int:
    tags = stream.get("tags", {})
    title = str(tags.get("title", ""))
    language = str(tags.get("language", ""))
    disposition = stream.get("disposition", {})
    score = 0
    if US_PATTERN.search(f"{language} {title}"):
        score += 100
    if SDH_PATTERN.search(title) or disposition.get("hearing_impaired"):
        score -= 35
    if FORCED_PATTERN.search(title) or disposition.get("forced"):
        score -= 60
    if disposition.get("default"):
        score += 5
    return score


def _score_sidecar(video: Path, sidecar: Path) -> int:
    suffix = sidecar.name[len(video.stem) + 1:-len(sidecar.suffix)]
    score = 20 + (15 if sidecar.suffix.lower() == ".srt" else 0)
    if US_PATTERN.search(suffix):
        score += 100
    if SDH_PATTERN.search(suffix):
        score -= 35
    if FORCED_PATTERN.search(suffix):
        score -= 60
    return score


def _has_persian_script(content: str) -> bool:
    arabic = sum("\u0600" <= character <= "\u06ff" for character in content)
    latin = sum("a" <= character.lower() <= "z" for character in content)
    return arabic >= 30 and arabic > latin


def _clearly_persian(content: str) -> bool:
    return _has_persian_script(content) and sum(character in "پچژگکی" for character in content) >= 10


def _subtitle_sample(video: Path, stream: dict) -> str:
    if stream.get("codec_name") not in {"subrip", "mov_text", "ass", "ssa", "webvtt"}:
        raise SubtitleNormalizationError(f"Cannot classify unlabeled subtitle stream {stream['index']} in {video}")
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(video), "-map", f"0:{stream['index']}",
         "-frames:s", "80", "-f", "srt", "-"],
        capture_output=True, timeout=90,
    )
    if result.returncode != 0:
        raise SubtitleNormalizationError(f"Cannot inspect subtitle stream {stream['index']} in {video}")
    return result.stdout.decode("utf-8", errors="replace")


def _check_english_sources(video: Path, embedded: list[dict], external: list[Path]) -> None:
    for path in external:
        if path.stat().st_size < 100:
            raise SubtitleNormalizationError(f"English-labeled sidecar is too short to verify: {path}")
        try:
            content = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise SubtitleNormalizationError(f"Cannot verify English-labeled sidecar: {path}") from exc
        if _has_persian_script(content):
            raise SubtitleNormalizationError(f"English-labeled sidecar appears Persian; left untouched: {path}")
    for stream in embedded:
        if stream.get("codec_name") not in {"subrip", "mov_text", "ass", "ssa", "webvtt"}:
            continue
        if _has_persian_script(_subtitle_sample(video, stream)):
            raise SubtitleNormalizationError(f"English-labeled stream {stream['index']} appears Persian; left untouched: {video}")


def _stream_signature(stream: dict) -> tuple:
    tags = stream.get("tags", {})
    return (
        stream.get("codec_type"), stream.get("codec_name"),
        tags.get("language", "und"), tags.get("title", ""),
        stream.get("width"), stream.get("height"), stream.get("pix_fmt"),
        stream.get("color_primaries"), stream.get("color_transfer"), stream.get("color_space"),
        stream.get("channels"), stream.get("sample_rate"),
    )


def _remux_without_streams(video: Path, original: dict, drop_indices: set[int], persian_indices: set[int]) -> None:
    source_stat = video.stat()
    if shutil.disk_usage(video.parent).free < source_stat.st_size + 1_000_000_000:
        raise SubtitleNormalizationError(f"Not enough free space to safely remux {video}")
    temporary = video.with_name(f".moviecatcher-subclear-{uuid.uuid4().hex}{video.suffix}")
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video), "-map", "0"]
    for index in sorted(drop_indices):
        command.extend(("-map", f"-0:{index}"))
    command.extend(("-map_metadata", "0", "-map_chapters", "0", "-c", "copy"))
    kept_subtitles = [
        stream["index"] for stream in original.get("streams", [])
        if stream.get("codec_type") == "subtitle" and stream["index"] not in drop_indices
    ]
    convert_subtitles: set[int] = set()
    if video.suffix.lower() in {".mp4", ".m4v", ".mov"}:
        for stream in original.get("streams", []):
            if stream["index"] in kept_subtitles and stream.get("codec_name") != "mov_text":
                if stream.get("codec_name") not in {"subrip", "ass", "ssa", "webvtt"}:
                    raise SubtitleNormalizationError(
                        f"Cannot safely retain subtitle codec {stream.get('codec_name')} in MP4: {video}"
                    )
                ordinal = kept_subtitles.index(stream["index"])
                command.extend((f"-c:s:{ordinal}", "mov_text"))
                convert_subtitles.add(stream["index"])
    for index in sorted(persian_indices):
        command.extend((f"-metadata:s:s:{kept_subtitles.index(index)}", "language=per"))
    if video.suffix.lower() in {".mp4", ".m4v", ".mov"}:
        # The MP4 muxer can synthesize a new timecode track from source metadata.
        command.extend(("-write_tmcd", "0"))
    command.append(str(temporary))
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=3600)
        if result.returncode != 0:
            raise SubtitleNormalizationError(f"FFmpeg remux failed for {video}: {result.stderr[-600:]}")
        checked = probe(temporary)
        expected = []
        for stream in original.get("streams", []):
            if stream["index"] in drop_indices:
                continue
            signature = list(_stream_signature(stream))
            if stream["index"] in persian_indices:
                signature[2] = "per"
            if stream["index"] in convert_subtitles:
                signature[1] = "mov_text"
            expected.append(tuple(signature))
        output_streams = list(checked.get("streams", []))
        # MP4 stores copied chapters as a synthetic QuickTime text data track.
        # It is not a selectable subtitle and must not count as a new media stream.
        if video.suffix.lower() in {".mp4", ".m4v", ".mov"} and original.get("chapters") and len(output_streams) == len(expected) + 1:
            synthetic = [
                stream for stream in output_streams
                if stream.get("codec_type") == "data" and stream.get("codec_tag_string") == "text"
                and stream.get("tags", {}).get("handler_name") == "SubtitleHandler"
            ]
            if len(synthetic) == 1:
                output_streams.remove(synthetic[0])
        actual = [_stream_signature(stream) for stream in output_streams]
        if video.suffix.lower() in {".mp4", ".m4v", ".mov"}:
            # MP4 does not round-trip Matroska stream title metadata.
            expected = [signature[:3] + ("",) + signature[4:] for signature in expected]
            actual = [signature[:3] + ("",) + signature[4:] for signature in actual]
        if actual != expected:
            mismatches = [(position, old, new) for position, (old, new) in enumerate(zip(expected, actual)) if old != new]
            raise SubtitleNormalizationError(
                f"Stream verification failed for {video}: expected {len(expected)} streams, got {len(actual)}; "
                f"first mismatch: {mismatches[:1]}"
            )
        before_chapters = original.get("chapters", [])
        after_chapters = checked.get("chapters", [])
        if len(before_chapters) != len(after_chapters) or any(
            abs(float(old["start_time"]) - float(new["start_time"])) > 1
            for old, new in zip(before_chapters, after_chapters)
        ):
            raise SubtitleNormalizationError(f"Chapter verification failed for {video}")
        before = float(original.get("format", {}).get("duration") or 0)
        after = float(checked.get("format", {}).get("duration") or 0)
        if before and abs(after - before) > 2:
            raise SubtitleNormalizationError(f"Duration changed unexpectedly for {video}")
        os.chmod(temporary, stat.S_IMODE(source_stat.st_mode))
        os.replace(temporary, video)
    finally:
        temporary.unlink(missing_ok=True)


def normalize_english_subtitles(video: Path) -> str:
    """Keep one English choice and every Persian subtitle; remove other languages."""
    video = Path(video)
    original = probe(video)
    embedded = english_streams(original)
    external = english_sidecars(video)
    if embedded or external:
        _check_english_sources(video, embedded, external)
    candidates = [
        (_score_stream(stream), -stream["index"], "embedded", stream)
        for stream in embedded
    ] + [
        (_score_sidecar(video, path), 0, "external", path)
        for path in external
    ]
    if candidates:
        _, _, selected_type, selected = max(candidates)
    else:
        selected_type, selected = None, None
    selected_index = selected["index"] if selected_type == "embedded" else None
    drop_indices = {stream["index"] for stream in embedded if stream["index"] != selected_index}
    persian_indices: set[int] = set()
    for stream in original.get("streams", []):
        if stream.get("codec_type") != "subtitle" or stream in embedded:
            continue
        language = str(stream.get("tags", {}).get("language", "")).lower()
        if language in PERSIAN_TAGS:
            continue
        if language in {"", "und"}:
            sample = _subtitle_sample(video, stream)
            if not sample.strip():
                raise SubtitleNormalizationError(f"Unlabeled subtitle stream {stream['index']} could not be classified: {video}")
            if _has_persian_script(sample):
                if _clearly_persian(sample):
                    persian_indices.add(stream["index"])
                else:
                    logger.warning("Ambiguous Arabic-script subtitle retained without relabeling: %s stream %s", video, stream["index"])
                continue
        drop_indices.add(stream["index"])
    remove_sidecars = []
    for path in associated_sidecars(video):
        language = _sidecar_language(video, path)
        if language == "persian" or (language == "english" and selected_type == "external" and path == selected):
            continue
        if language == "unknown":
            try:
                content = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError) as exc:
                raise SubtitleNormalizationError(f"Unlabeled sidecar could not be classified: {path}") from exc
            if _has_persian_script(content):
                logger.warning("Unlabeled Persian-looking sidecar retained: %s", path)
                continue
        remove_sidecars.append(path)
    if not drop_indices and not remove_sidecars and not persian_indices:
        return "unchanged"
    if drop_indices or persian_indices:
        _remux_without_streams(video, original, drop_indices, persian_indices)
    for path in remove_sidecars:
        path.unlink()
        logger.info("Removed unneeded subtitle sidecar: %s", path)
    logger.info("Kept English=%s and Persian subtitles for %s", selected_type or "none", video)
    return "normalized"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("scan", "normalize"))
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    from subtitles import configure_error_log
    configure_error_log(Path(os.getenv("SESSION_DIR", "/data")))
    paths = [args.path] if args.path.is_file() else sorted(
        path for path in args.path.rglob("*")
        if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES and not path.name.startswith("._")
    )
    counts = {"checked": 0, "multiple_english": 0, "needs_cleanup": 0, "normalized": 0, "failed": 0}
    for video in paths:
        try:
            info = probe(video)
            embedded = english_streams(info)
            count = len(embedded) + len(english_sidecars(video))
            other = any(
                stream.get("codec_type") == "subtitle"
                and str(stream.get("tags", {}).get("language", "")).lower() not in ENGLISH_TAGS | PERSIAN_TAGS
                for stream in info.get("streams", [])
            )
            counts["checked"] += 1
            if count > 1:
                counts["multiple_english"] += 1
            if count > 1 or other or any(_sidecar_language(video, path) not in {"english", "persian"} for path in associated_sidecars(video)):
                counts["needs_cleanup"] += 1
                if args.action == "normalize":
                    result = normalize_english_subtitles(video)
                    if result == "normalized":
                        counts["normalized"] += 1
                        print(f"NORMALIZED\t{video}", flush=True)
                else:
                    print(f"NEEDS_CLEANUP\tEnglish choices={count}\t{video}", flush=True)
        except Exception:
            logger.exception("English subtitle normalization failed for %s", video)
            counts["failed"] += 1
    print(f"Summary: {counts}", flush=True)
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
