"""Download matching English sidecar subtitles without changing the video file."""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from difflib import SequenceMatcher
from pathlib import Path

from guessit import guessit

from dolby_vision import VIDEO_SUFFIXES

logger = logging.getLogger(__name__)
_SRT_TIME = re.compile(r"(?m)^\s*(\d{2}):(\d{2}):(\d{2})[,\.]\d{3}\s*-->")
_SUBDL_API = "https://api.subdl.com/api/v1/subtitles"
_SUBDL_DOWNLOAD = "https://dl.subdl.com"


def _probe(video: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(video)],
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
    times = [int(h) * 3600 + int(m) * 60 + int(s) for h, m, s in _SRT_TIME.findall(content)]
    if len(times) < 10 or times != sorted(times):
        return False
    if duration and duration > 600 and not 0.55 * duration <= times[-1] <= duration + 120:
        return False
    return True


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
        "subs_per_page": "30", "releases": "1", "unpack": "1",
        "client": "custom_integration",
    }
    if video_info.get("type") == "episode":
        parameters.update(type="tv", season_number=video_info.get("season"), episode_number=video_info.get("episode"))
    else:
        parameters["type"] = "movie"
    url = f"{_SUBDL_API}?{urllib.parse.urlencode(parameters)}"
    with urllib.request.urlopen(url, timeout=20) as response:
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
            if file.get("language", "EN").upper() != "EN":
                continue
            release = file.get("release_name") or item.get("release_name") or ""
            score = _release_score(video_info, release, file if file.get("season") else item)
            if score >= (75 if video_info.get("type") == "episode" else 65):
                scored.append((score, file.get("url") or item.get("url"), file.get("name") or item.get("name")))
    for _, subtitle_url, name in sorted(scored, reverse=True)[:3]:
        if not subtitle_url or not subtitle_url.startswith("/subtitle/"):
            continue
        with urllib.request.urlopen(f"{_SUBDL_DOWNLOAD}{subtitle_url}", timeout=20) as response:
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


def fetch_english_subtitle(video: Path) -> str:
    """Return downloaded, external, or unavailable.

    Only a reasonably matched, readable SRT is published; an unmatched video is
    left untouched for a later retry or manual selection.
    """
    video = Path(video)
    if video.suffix.lower() not in VIDEO_SUFFIXES or video.name.startswith("._"):
        return "unavailable"
    sidecar = video.with_name(f"{video.stem}.en.srt")
    if sidecar.exists():
        return "external"
    info = _probe(video)
    duration = float(info.get("format", {}).get("duration") or 0) or None
    with tempfile.TemporaryDirectory(prefix=".moviecatcher-subtitles-", dir=video.parent) as temp:
        candidate = Path(temp) / sidecar.name
        api_key = os.getenv("SUBDL_API_KEY", "").strip()
        if api_key:
            try:
                if _fetch_subdl(video, candidate, duration, api_key):
                    try:
                        os.link(candidate, sidecar)
                    except FileExistsError:
                        return "external"
                    logger.info("English subtitle saved from SubDL: %s", sidecar)
                    return "downloaded"
            except Exception as exc:
                # HTTP exceptions can contain the query-string API key.
                logger.warning("SubDL lookup failed for %s (%s)", video, type(exc).__name__)
        # No unauthenticated movie provider is enabled; movie lookups use SubDL.
        if guessit(video.name).get("type") != "episode":
            return "unavailable"
        providers = ("gestdown", "tvsubtitles")
        command = [sys.executable, "-m", "subliminal", "download", "-l", "en"]
        for provider in providers:
            command.extend(("-p", provider))
        command.extend(("-r", "hash", "-r", "metadata", "-m", "60", "-F", "srt", "-d", temp, str(video)))
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        if result.returncode != 0 or not _valid_srt(candidate, duration):
            logger.warning("English subtitle unavailable for %s: %s", video, (result.stderr or result.stdout)[-600:])
            return "unavailable"
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
    args = parser.parse_args()
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
    return 0 if counts.get("error", 0) == 0 else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
