"""Remove Dolby Vision RPU metadata without re-encoding HDR10 video or audio."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import uuid
from pathlib import Path

VIDEO_SUFFIXES = frozenset({
    ".mkv", ".mp4", ".m4v", ".mov", ".ts", ".m2ts", ".mts", ".avi", ".webm"
})


class DolbyVisionError(RuntimeError):
    """The source was left intact because safe removal could not be verified."""


def probe(path: Path, ffprobe: str = "ffprobe") -> dict:
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _video_streams(info: dict) -> list[dict]:
    return [stream for stream in info.get("streams", []) if stream.get("codec_type") == "video"]


def _dolby_side_data(stream: dict) -> dict | None:
    return next(
        (
            entry
            for entry in stream.get("side_data_list", [])
            if entry.get("side_data_type") == "DOVI configuration record"
            and int(entry.get("rpu_present_flag", 0)) == 1
        ),
        None,
    )


def has_dolby_vision(info: dict) -> bool:
    return any(_dolby_side_data(stream) is not None for stream in _video_streams(info))


def _verify(source: dict, result: dict) -> None:
    old_streams = source.get("streams", [])
    new_streams = result.get("streams", [])
    if len(old_streams) != len(new_streams):
        raise DolbyVisionError("The output changed the number of media streams")
    for old, new in zip(old_streams, new_streams):
        for key in ("codec_type", "codec_name", "width", "height", "channels"):
            if old.get(key) != new.get(key):
                raise DolbyVisionError(f"The output changed stream {key}")
        if old.get("tags", {}).get("language") != new.get("tags", {}).get("language"):
            raise DolbyVisionError("The output changed an audio or subtitle language")
        if old.get("codec_type") == "video":
            for key in ("color_transfer", "color_primaries", "color_space"):
                if old.get(key) != new.get(key):
                    raise DolbyVisionError(f"The output changed video {key}")
    if has_dolby_vision(result):
        raise DolbyVisionError("Dolby Vision metadata is still present in the output")
    try:
        old_duration = float(source["format"]["duration"])
        new_duration = float(result["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        pass
    else:
        if abs(old_duration - new_duration) > 2:
            raise DolbyVisionError("The output duration differs from the source")


def remove_dolby_vision(
    path: Path, *, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe"
) -> bool:
    """Atomically replace a profile-8 DV file with its HDR10 base; return False if absent.

    The temporary output is in the same directory. No backup is retained. Any
    conversion or verification error leaves the original file in place.
    """
    path = Path(path)
    if path.suffix.lower() not in VIDEO_SUFFIXES:
        return False
    original_stat = path.stat()
    source = probe(path, ffprobe)
    dv_streams = [
        (number, stream, _dolby_side_data(stream))
        for number, stream in enumerate(_video_streams(source))
    ]
    dv_streams = [(n, s, data) for n, s, data in dv_streams if data is not None]
    if not dv_streams:
        return False
    for _, stream, data in dv_streams:
        if (
            stream.get("codec_name") != "hevc"
            or stream.get("color_transfer") != "smpte2084"
            or int(data.get("dv_profile", -1)) != 8
            or int(data.get("bl_present_flag", 0)) != 1
        ):
            raise DolbyVisionError("This Dolby Vision profile has no verified HDR10 base")

    temporary = path.with_name(f".{path.stem}.dv-removing-{uuid.uuid4().hex}{path.suffix}")
    command = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
               "-i", str(path), "-map", "0", "-map_metadata", "0",
               "-map_chapters", "0", "-c", "copy"]
    for number, _, _ in dv_streams:
        command.extend([f"-bsf:v:{number}", "dovi_rpu=strip=1"])
        if path.suffix.lower() in {".mp4", ".m4v", ".mov"}:
            command.extend([f"-tag:v:{number}", "hvc1"])
    command.append(str(temporary))
    try:
        subprocess.run(command, capture_output=True, text=True, check=True)
        converted = probe(temporary, ffprobe)
        _verify(source, converted)
        if temporary.stat().st_size == 0:
            raise DolbyVisionError("The output is empty")
        current_stat = path.stat()
        if (current_stat.st_size, current_stat.st_mtime_ns) != (
            original_stat.st_size, original_stat.st_mtime_ns
        ):
            raise DolbyVisionError("The source changed while it was being processed")
        os.chmod(temporary, stat.S_IMODE(original_stat.st_mode))
        os.replace(temporary, path)
        return True
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()[-600:]
        raise DolbyVisionError(f"FFmpeg could not remove Dolby Vision: {detail}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] not in {"scan", "remove"}:
        print("Usage: dolby_vision.py scan|remove FILE_OR_DIRECTORY", file=sys.stderr)
        return 2
    root = Path(sys.argv[2])
    if not root.exists():
        print(f"Path does not exist: {root}", file=sys.stderr)
        return 2
    files = (
        [root]
        if root.is_file()
        else sorted(
            path for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES
            and not any(part.startswith(".") for part in path.relative_to(root).parts)
        )
    )
    failures = 0
    for path in files:
        try:
            if sys.argv[1] == "scan":
                if has_dolby_vision(probe(path)):
                    print(f"DOLBY_VISION {path}", flush=True)
            elif remove_dolby_vision(path):
                print(f"REMOVED {path}", flush=True)
        except (DolbyVisionError, OSError, subprocess.CalledProcessError, ValueError) as exc:
            failures += 1
            print(f"FAILED {path}: {exc}", file=sys.stderr, flush=True)
    print(f"Checked {len(files)} files; failures: {failures}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
