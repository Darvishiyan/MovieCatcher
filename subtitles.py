"""Download matching English sidecar subtitles without changing the video file."""

from __future__ import annotations

import argparse
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
import urllib.parse
import urllib.error
import urllib.request
import zipfile
from difflib import SequenceMatcher
from pathlib import Path

from guessit import guessit
import pysubs2

from dolby_vision import VIDEO_SUFFIXES
from subtitle_normalize import _has_persian_script, english_sidecars, english_streams

logger = logging.getLogger(__name__)
_SRT_TIME = re.compile(r"(?m)^\s*(\d{2}):(\d{2}):(\d{2})[,\.]\d{3}\s*-->")
_SUBDL_API = "https://api.subdl.com/api/v1/subtitles"
_SUBDL_DOWNLOAD = "https://dl.subdl.com"
_next_subdl_download_at = 0.0


class SubtitleRateLimited(RuntimeError):
    """The subtitle provider has temporarily exhausted its download quota."""


class _PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        descriptor = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.chmod(self.baseFilename, 0o600)
        return os.fdopen(descriptor, "a", encoding=self.encoding or "utf-8")


def configure_error_log(directory: Path) -> Path:
    """Keep warnings and errors across container restarts, with bounded size."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "moviecatcher-errors.log"
    root = logging.getLogger()
    if not any(isinstance(handler, _PrivateRotatingFileHandler) and handler.baseFilename == str(path) for handler in root.handlers):
        handler = _PrivateRotatingFileHandler(path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        handler.setLevel(logging.WARNING)
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        root.addHandler(handler)
    return path


def _subdl_open(url: str):
    global _next_subdl_download_at
    if time.time() < _next_subdl_download_at:
        raise SubtitleRateLimited("SubDL download quota has not reset")
    try:
        return urllib.request.urlopen(url, timeout=20)
    except urllib.error.HTTPError as exc:
        if exc.code != 429:
            raise
        try:
            retry_after = max(60, int(exc.headers.get("Retry-After", "3600")))
        except (TypeError, ValueError):
            retry_after = 3600
        _next_subdl_download_at = time.time() + retry_after
        raise SubtitleRateLimited(f"SubDL download quota reached; retry after {retry_after}s") from None


def _probe(video: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(video)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(result.stdout)


def _valid_srt(path: Path, duration: float | None) -> bool:
    if not path.is_file() or path.stat().st_size < 100:
        return False
    try:
        content = path.read_text(encoding="utf-8-sig")
    except (UnicodeError, OSError):
        return False
    if _has_persian_script(content):
        return False
    times = [int(h) * 3600 + int(m) * 60 + int(s) for h, m, s in _SRT_TIME.findall(content)]
    if len(times) < 10 or times != sorted(times):
        return False
    if duration and duration > 600 and not 0.8 * duration <= times[-1] <= duration + 120:
        return False
    return True


def _repair_srt_order(path: Path, duration: float | None) -> bool:
    """Sort a few misplaced cues without changing their text or timestamps."""
    try:
        subs = pysubs2.load(str(path), encoding="utf-8-sig")
    except Exception:
        return False
    inversions = sum(right.start < left.start for left, right in zip(subs.events, subs.events[1:]))
    if len(subs.events) < 10 or not 0 < inversions <= 5:
        return False
    subs.events.sort(key=lambda event: (event.start, event.end))
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.srt")
    try:
        subs.save(str(temporary), format_="srt", encoding="utf-8")
        if not _valid_srt(temporary, duration):
            return False
        os.replace(temporary, path)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _normalized(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _release_score(video_info: dict, release: str, item: dict) -> int:
    """Reject other titles/cuts; favor matching release properties."""
    if not release:
        return -1
    candidate = guessit(release)
    if SequenceMatcher(None, _normalized(video_info.get("title")), _normalized(candidate.get("title"))).ratio() < 0.82:
        return -1
    score = 35
    is_episode = video_info.get("type") == "episode"
    if is_episode:
        season = item.get("season") or candidate.get("season")
        episode = item.get("episode") or candidate.get("episode")
        if season != video_info.get("season") or episode != video_info.get("episode"):
            return -1
        score += 40
    elif video_info.get("year"):
        if candidate.get("year") != video_info["year"]:
            return -1
        score += 20
    source = video_info.get("source")
    if source and candidate.get("source") == source:
        score += 20
    elif source and candidate.get("source"):
        return -1
    if video_info.get("screen_size") and candidate.get("screen_size") == video_info["screen_size"]:
        score += 10
    if video_info.get("release_group") and candidate.get("release_group") == video_info["release_group"]:
        score += 20
    return score


def _fetch_subdl(video: Path, candidate_path: Path, duration: float | None, api_key: str) -> bool:
    """Search SubDL with the user's own free key and inspect up to three releases."""
    video_info = dict(guessit(video.name))
    parameters = {
        "api_key": api_key, "file_name": video.name, "languages": "EN",
        "subs_per_page": "30", "releases": "1", "unpack": "1", "hi": "1",
        "client": "custom_integration",
    }
    if video_info.get("type") == "episode":
        parameters.update(type="tv", season_number=video_info.get("season"), episode_number=video_info.get("episode"))
    else:
        parameters["type"] = "movie"
    url = f"{_SUBDL_API}?{urllib.parse.urlencode(parameters)}"
    with _subdl_open(url) as response:
        data = json.load(response)
    if not data.get("status"):
        raise RuntimeError(f"SubDL search failed: {data.get('error', 'unknown error')}")
    results = data.get("results") or []
    if not results or SequenceMatcher(None, _normalized(video_info.get("title")), _normalized(results[0].get("name"))).ratio() < 0.82:
        return False
    if video_info.get("year") and results[0].get("year") and results[0]["year"] != video_info["year"]:
        return False
    scored = []
    for item in data.get("subtitles") or []:
        if item.get("full_season") and not item.get("unpack_files"):
            continue
        files = item.get("unpack_files") or [item]
        for file in files:
            language = str(file.get("language", "EN")).upper()
            if language not in {"EN", "EN-US", "EN_US"}:
                continue
            release = file.get("release_name") or item.get("release_name") or ""
            score = _release_score(video_info, release, file if file.get("season") else item)
            if score >= (75 if video_info.get("type") == "episode" else 65):
                label = f"{release} {file.get('name') or item.get('name') or ''}"
                if language in {"EN-US", "EN_US"} or re.search(r"(?i)\b(?:english\s*\(?us\)?|american|en[-_]us)\b", label):
                    score += 15
                if file.get("hi", item.get("hi")) or re.search(r"(?i)\b(?:sdh|hearing.impaired|cc)\b", label):
                    score -= 10
                if re.search(r"(?i)\b(?:forced|foreign)\b", label):
                    score -= 20
                scored.append((score, file.get("url") or item.get("url"), file.get("name") or item.get("name")))
    for _, subtitle_url, name in sorted(scored, reverse=True)[:3]:
        if not subtitle_url or not subtitle_url.startswith("/subtitle/"):
            continue
        with _subdl_open(f"{_SUBDL_DOWNLOAD}{subtitle_url}") as response:
            body = response.read(10_000_001)
        if len(body) > 10_000_000:
            continue
        if body.startswith(b"PK"):
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                members = [entry for entry in archive.infolist() if entry.filename.lower().endswith(".srt") and entry.file_size < 2_000_000]
                if len(members) != 1:
                    continue
                body = archive.read(members[0])
        elif not str(name).lower().endswith(".srt"):
            continue
        try:
            text = body.decode("utf-8-sig")
        except UnicodeError:
            continue
        candidate_path.write_text(text, encoding="utf-8")
        if _valid_srt(candidate_path, duration):
            return True
    return False


def _fetch_other_providers(video: Path, candidate_path: Path, duration: float | None) -> bool:
    """Try multiple public releases when SubDL has no valid result."""
    providers = ["opensubtitles"]
    video_info = guessit(video.name)
    if video_info.get("type") == "episode":
        providers.extend(("gestdown", "tvsubtitles"))
    ignored_ids: list[str] = []
    for _ in range(5):
        candidate_path.unlink(missing_ok=True)
        command = [sys.executable, "-m", "subliminal", "--debug", "download", "-l", "en"]
        for provider in providers:
            command.extend(("-p", provider))
        command.extend((
            "-r", "hash", "-r", "metadata", "-m",
            "60" if video_info.get("year") or video_info.get("type") == "episode" else "80",
            "-W", "-C", "n,hi",
            "-F", "srt", "-d", str(candidate_path.parent),
        ))
        for subtitle_id in ignored_ids:
            command.extend(("-I", subtitle_id))
        command.append(str(video))
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            logger.warning("Public subtitle provider failed for %s (exit %s)", video, result.returncode)
            return False
        log = result.stdout + "\n" + result.stderr
        ids = re.findall(r"Saving <OpenSubtitlesSubtitle '([^']+)'", log)
        subtitle_id = ids[-1] if ids else None
        match_details = re.findall(r"English subtitle from opensubtitles \(match on ([^)]*)\)", log)
        matches = {part.strip() for part in match_details[-1].split(",")} if match_details else set()
        valid = _valid_srt(candidate_path, duration)
        if not valid and candidate_path.is_file():
            valid = _repair_srt_order(candidate_path, duration)
        if subtitle_id and video_info.get("year") and not {"title", "year"} <= matches:
            valid = False
        if subtitle_id and video_info.get("type") == "episode" and not (
            {"season", "episode"} <= matches or "hash" in matches
        ):
            valid = False
        if valid:
            return True
        if not subtitle_id or subtitle_id in ignored_ids:
            return False
        logger.info("Rejected mismatched or incomplete subtitle candidate %s for %s", subtitle_id, video)
        ignored_ids.append(subtitle_id)
    return False


def fetch_english_subtitle(video: Path) -> str:
    """Return downloaded, external, embedded, rate_limited, or unavailable.

    Only a reasonably matched, readable SRT is published; an unmatched video is
    left untouched for a later retry or manual selection.
    """
    video = Path(video)
    if video.suffix.lower() not in VIDEO_SUFFIXES or video.name.startswith("._"):
        return "unavailable"
    sidecar = video.with_name(f"{video.stem}.en.srt")
    if english_sidecars(video):
        return "external"
    info = _probe(video)
    if english_streams(info):
        return "embedded"
    duration = float(info.get("format", {}).get("duration") or 0) or None
    with tempfile.TemporaryDirectory(prefix=".moviecatcher-subtitles-", dir=video.parent) as temp:
        candidate = Path(temp) / sidecar.name
        api_key = os.getenv("SUBDL_API_KEY", "").strip()
        rate_limited = False
        if api_key:
            try:
                if _fetch_subdl(video, candidate, duration, api_key):
                    try:
                        os.link(candidate, sidecar)
                    except FileExistsError:
                        return "external"
                    logger.info("English subtitle saved from SubDL: %s", sidecar)
                    return "downloaded"
            except SubtitleRateLimited:
                rate_limited = True
                logger.warning("SubDL download quota reached for %s; retry later", video)
            except Exception as exc:
                # HTTP exceptions can contain the query-string API key.
                logger.warning("SubDL lookup failed for %s (%s)", video, type(exc).__name__)
        if not _fetch_other_providers(video, candidate, duration):
            return "rate_limited" if rate_limited else "unavailable"
        try:
            os.link(candidate, sidecar)
        except FileExistsError:
            return "external"
    logger.info("English subtitle saved: %s", sidecar)
    return "downloaded"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("scan", "one"))
    parser.add_argument("path", type=Path)
    parser.add_argument("--error-log-dir", type=Path, default=Path(os.getenv("SESSION_DIR", "/data")))
    args = parser.parse_args()
    configure_error_log(args.error_log_dir)
    paths = (
        sorted(path for path in args.path.rglob("*") if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES and not path.name.startswith("._"))
        if args.action == "scan" else [args.path]
    )
    counts: dict[str, int] = {}
    for path in paths:
        try:
            result = fetch_english_subtitle(path)
        except Exception as exc:
            logger.exception("Subtitle check failed for %s", path)
            result = "error"
        counts[result] = counts.get(result, 0) + 1
        print(f"{result}\t{path}", flush=True)
    print(f"Summary: {counts}", flush=True)
    return 75 if counts.get("rate_limited", 0) else (1 if counts.get("error", 0) else 0)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
