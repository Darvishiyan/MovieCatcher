"""Restricted server-side operations for Moviecatcher's Windows converter."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

MEDIA = Path("/srv/storage/media")
DATA = Path("/opt/moviecatcher/data/converter")
QUEUE = DATA / "queue"
DONE = DATA / "done"
INCOMING = MEDIA / ".moviecatcher-incoming"
BACKUP = MEDIA / ".moviecatcher-originals"
VALID_ID = re.compile(r"^[0-9a-f]{32}$")
PROFILES = {"pending", "hdr10-copy", "hdr10-basic", "h264-4k-sdr", "h264-1080p-sdr", "passthrough"}


def job(job_id: str) -> tuple[Path, dict, Path]:
    if not VALID_ID.fullmatch(job_id):
        raise ValueError("Invalid job ID")
    marker = QUEUE / f"{job_id}.json"
    record = json.loads(marker.read_text(encoding="utf-8"))
    if record.get("version") != 1 or record.get("id") != job_id:
        raise ValueError("Invalid job record")
    relative = Path(record["relative_path"])
    if not relative.parts or relative.is_absolute() or ".." in relative.parts or relative.parts[0].startswith("."):
        raise ValueError("Invalid media path")
    source = (MEDIA / relative).resolve(strict=True)
    source.relative_to(MEDIA)
    if not source.is_file() or source.stat().st_size != record["size"]:
        raise ValueError("Source changed after enqueue")
    return marker, record, source


def save(marker: Path, record: dict) -> None:
    temporary = marker.with_name(f".{uuid.uuid4().hex}.tmp")
    with temporary.open("x", encoding="utf-8") as output:
        json.dump(record, output, ensure_ascii=False)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, marker)


def incoming_path(job_id: str) -> Path:
    if not VALID_ID.fullmatch(job_id):
        raise ValueError("Invalid job ID")
    return INCOMING / f"{job_id}.mp4.part"


def archive(marker: Path, record: dict, status: str) -> None:
    DONE.mkdir(parents=True, exist_ok=True)
    record["status"] = status
    record["finished"] = time.time()
    save(marker, record)
    os.replace(marker, DONE / marker.name)


def run(command: str, arguments: list[str]) -> None:
    if command == "status":
        print(json.dumps({"server": "ready", "queue": str(QUEUE), "media": str(MEDIA)}))
    elif command == "list":
        QUEUE.mkdir(parents=True, exist_ok=True)
        records = []
        for marker in sorted(QUEUE.glob("*.json")):
            try:
                _, record, _ = job(marker.stem)
                records.append(record)
            except (ValueError, FileNotFoundError, KeyError) as exc:
                print(f"Ignoring {marker.name}: {exc}", file=sys.stderr)
        print(json.dumps(records, ensure_ascii=False))
    elif command == "enqueue":
        relative = Path(arguments[0])
        if not relative.parts or relative.is_absolute() or ".." in relative.parts or relative.parts[0].startswith("."):
            raise ValueError("Invalid relative path")
        source = (MEDIA / relative).resolve(strict=True)
        source.relative_to(MEDIA)
        if not source.is_file():
            raise ValueError("Not a file")
        QUEUE.mkdir(parents=True, exist_ok=True)
        job_id = uuid.uuid4().hex
        profile = arguments[1] if len(arguments) > 1 else "pending"
        mode = arguments[2] if len(arguments) > 2 else "replace"
        if profile not in PROFILES or mode not in {"replace", "test"}:
            raise ValueError("Unsupported profile or mode")
        record = {"version": 1, "id": job_id, "relative_path": relative.as_posix(),
                  "size": source.stat().st_size, "created": time.time(),
                  "profile": profile, "mode": mode}
        if len(arguments) > 3:
            sample_start = int(arguments[3])
            sample_seconds = int(arguments[4])
            if mode != "test" or sample_start < 0 or not 10 <= sample_seconds <= 600:
                raise ValueError("Invalid test sample window")
            record["sample_start"] = sample_start
            record["sample_seconds"] = sample_seconds
        save(QUEUE / f"{job_id}.json", record)
        print(json.dumps(record, ensure_ascii=False))
    elif command == "set-profile":
        marker, record, _ = job(arguments[0])
        profile = arguments[1]
        if profile not in PROFILES:
            raise ValueError("Unsupported profile")
        record["profile"] = profile
        save(marker, record)
        print(json.dumps(record, ensure_ascii=False))
    elif command == "probe":
        _, _, source = job(arguments[0])
        path_in_container = Path("/media") / source.relative_to(MEDIA)
        result = subprocess.run(
            ["docker", "exec", "jellyfin", "/usr/lib/jellyfin-ffmpeg/ffprobe",
             "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path_in_container)],
            capture_output=True, check=True, text=True,
        )
        print(result.stdout)
    elif command == "stream":
        _, _, source = job(arguments[0])
        offset = int(arguments[1])
        if offset < 0 or offset > source.stat().st_size:
            raise ValueError("Invalid offset")
        with source.open("rb") as input_file:
            input_file.seek(offset)
            shutil.copyfileobj(input_file, sys.stdout.buffer, 1024 * 1024)
    elif command == "upload-size":
        job(arguments[0])
        path = incoming_path(arguments[0])
        print(path.stat().st_size if path.exists() else 0)
    elif command == "receive":
        job(arguments[0])
        path = incoming_path(arguments[0])
        INCOMING.mkdir(parents=True, exist_ok=True)
        offset = int(arguments[1])
        existing = path.stat().st_size if path.exists() else 0
        if offset != existing or offset < 0:
            raise ValueError("Upload offset mismatch")
        with path.open("ab") as output:
            shutil.copyfileobj(sys.stdin.buffer, output, 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
    elif command == "complete":
        marker, record, source = job(arguments[0])
        expected_hash = arguments[1]
        expected_size = int(arguments[2])
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ValueError("Invalid SHA256")
        uploaded = incoming_path(arguments[0])
        if not uploaded.is_file() or uploaded.stat().st_size != expected_size:
            raise ValueError("Upload size mismatch")
        with uploaded.open("rb") as input_file:
            digest = hashlib.file_digest(input_file, "sha256").hexdigest()
        if digest != expected_hash:
            raise ValueError("Upload SHA256 mismatch")
        relative = Path(record["relative_path"])
        mode = record.get("mode", "replace")
        if mode == "test":
            destination = source.with_name(f"{source.stem}.{record['profile']}.WebOS-Test.mp4")
        elif mode == "replace":
            destination = source.with_suffix(".mp4")
        else:
            raise ValueError("Unsupported output mode")
        backup = BACKUP / relative
        if destination != source and destination.exists():
            raise FileExistsError(f"Output exists: {destination}")
        if mode == "replace" and backup.exists():
            raise FileExistsError(f"Backup exists: {backup}")
        if mode == "replace":
            backup.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, backup)
            try:
                os.replace(uploaded, destination)
            except BaseException:
                os.replace(backup, source)
                raise
            record["backup"] = str(backup.relative_to(MEDIA))
        else:
            os.replace(uploaded, destination)
        record["output"] = str(destination.relative_to(MEDIA))
        archive(marker, record, "completed")
        print(json.dumps(record, ensure_ascii=False))
    elif command == "skip":
        marker, record, _ = job(arguments[0])
        record["reason"] = arguments[1]
        archive(marker, record, "skipped")
        print(json.dumps(record, ensure_ascii=False))
    else:
        raise ValueError("Unknown command")


if __name__ == "__main__":
    try:
        run(sys.argv[1], sys.argv[2:])
    except Exception as exc:
        print(f"converter operation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
