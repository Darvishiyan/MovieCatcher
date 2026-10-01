"""Windows pull worker for MovieCatcher conversion jobs on a home server."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

HOST = os.getenv("MOVIECATCHER_HOST", "homeserver")
SERVER_SCRIPT = "/opt/moviecatcher/data/converter/server_ops.py"
WORK = Path(os.getenv("MOVIECATCHER_WORK", str(Path.home() / "MovieCatcherConverter")))
FFMPEG = os.getenv("MOVIECATCHER_FFMPEG", "ffmpeg")
FFPROBE = os.getenv("MOVIECATCHER_FFPROBE", "ffprobe")
SSH_OPTIONS = ["-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
               "-o", "StrictHostKeyChecking=yes", "-o", "ServerAliveInterval=15",
               "-o", "ServerAliveCountMax=3"]
LOG = logging.getLogger("moviecatcher-converter")


def remote_command(*arguments: object) -> list[str]:
    words = ["python3", SERVER_SCRIPT, *(str(item) for item in arguments)]
    return ["ssh", *SSH_OPTIONS, HOST, " ".join(shlex.quote(word) for word in words)]


def remote_json(*arguments: object) -> object:
    result = subprocess.run(remote_command(*arguments), capture_output=True, check=True)
    return json.loads(result.stdout.decode("utf-8"))


def ffprobe(path: Path) -> dict:
    result = subprocess.run(
        [FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, check=True,
    )
    return json.loads(result.stdout.decode("utf-8"))


def streams(probe: dict, kind: str) -> list[dict]:
    return [stream for stream in probe.get("streams", []) if stream.get("codec_type") == kind]


def pull(job_id: str, expected_size: int, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.stat().st_size > expected_size:
        destination.unlink()
    offset = destination.stat().st_size if destination.exists() else 0
    if offset == expected_size:
        return
    LOG.info("Receiving %s from byte %d of %d", job_id, offset, expected_size)
    process = subprocess.Popen(remote_command("stream", job_id, offset), stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    assert process.stdout is not None
    with destination.open("ab") as output:
        while chunk := process.stdout.read(1024 * 1024):
            output.write(chunk)
    error = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
    if process.wait() != 0 or destination.stat().st_size != expected_size:
        raise RuntimeError(f"Download interrupted at {destination.stat().st_size}: {error}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def command_for(job: dict, source: Path, output: Path, source_probe: dict) -> list[str]:
    profile = job["profile"]
    command = [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "warning", "-y"]
    sample_start = job.get("sample_start")
    sample_seconds = job.get("sample_seconds")
    if sample_start is not None:
        command += ["-ss", str(sample_start)]
    command += ["-i", str(source), "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?",
                "-map_metadata", "0", "-map_chapters", "0"]
    if sample_seconds is not None:
        command += ["-t", str(sample_seconds)]
    if profile in {"hdr10-copy", "hdr10-basic", "passthrough"}:
        command += ["-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text"]
        if profile in {"hdr10-copy", "hdr10-basic"}:
            command += ["-tag:v", "hvc1", "-bsf:v", "dovi_rpu=strip=1"]
        if profile == "hdr10-basic":
            command[-1] += ",filter_units=remove_types=39|40"
    elif profile in {"h264-4k-sdr", "h264-1080p-sdr"}:
        video = streams(source_probe, "video")[0]
        transfer = video.get("color_transfer", "")
        if transfer in {"smpte2084", "arib-std-b67"}:
            filters = ["zscale=t=linear:npl=100", "format=gbrpf32le", "tonemap=tonemap=mobius:desat=0",
                       "zscale=p=bt709:t=bt709:m=bt709:r=tv"]
        else:
            filters = []
        if profile == "h264-1080p-sdr":
            filters.append("scale=1920:-2")
        filters.append("format=yuv420p")
        command += ["-vf", ",".join(filters), "-c:v", "h264_nvenc", "-preset", "p5",
                    "-rc", "vbr", "-cq", "17", "-b:v", "0", "-c:a", "aac",
                    "-profile:a", "aac_low", "-b:a", "256k", "-c:s", "mov_text",
                    "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
    else:
        raise ValueError(f"Profile is not ready: {profile}")
    command += ["-movflags", "+faststart", str(output)]
    return command


def verify_output(job: dict, original: dict, converted: dict) -> None:
    input_video = streams(original, "video")
    output_video = streams(converted, "video")
    if len(input_video) < 1 or len(output_video) != 1:
        raise ValueError("Missing video in output")
    source_audio = streams(original, "audio")
    result_audio = streams(converted, "audio")
    if len(source_audio) != len(result_audio):
        raise ValueError(f"Audio track count changed: {len(source_audio)} -> {len(result_audio)}")
    for index, (old, new) in enumerate(zip(source_audio, result_audio)):
        old_language = old.get("tags", {}).get("language", "und")
        new_language = new.get("tags", {}).get("language", "und")
        if old_language != new_language:
            raise ValueError(f"Audio language changed on track {index}: {old_language} -> {new_language}")
    if len(streams(original, "subtitle")) != len(streams(converted, "subtitle")):
        raise ValueError("Subtitle track count changed")
    if job["profile"] in {"hdr10-copy", "hdr10-basic", "passthrough"}:
        if input_video[0].get("codec_name") != output_video[0].get("codec_name"):
            raise ValueError("Video codec changed during copy")
        if (input_video[0].get("width"), input_video[0].get("height")) != (
            output_video[0].get("width"), output_video[0].get("height")):
            raise ValueError("Video resolution changed during copy")
    else:
        if output_video[0].get("codec_name") != "h264":
            raise ValueError("Unexpected SDR video codec")
    expected = float(job.get("sample_seconds") or original["format"]["duration"])
    actual = float(converted["format"]["duration"])
    if abs(expected - actual) > 4:
        raise ValueError(f"Duration differs: {expected:.1f}s -> {actual:.1f}s")


def upload(job_id: str, path: Path) -> None:
    total = path.stat().st_size
    offset = int(subprocess.run(remote_command("upload-size", job_id), capture_output=True,
                                check=True).stdout.strip())
    if offset > total:
        raise ValueError("Server has more bytes than local output")
    if offset == total:
        return
    LOG.info("Returning %s from byte %d of %d", job_id, offset, total)
    process = subprocess.Popen(remote_command("receive", job_id, offset), stdin=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    assert process.stdin is not None
    try:
        with path.open("rb") as source:
            source.seek(offset)
            while chunk := source.read(1024 * 1024):
                process.stdin.write(chunk)
    finally:
        process.stdin.close()
    error = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
    if process.wait() != 0:
        raise RuntimeError(f"Upload interrupted: {error}")


def process(job: dict) -> None:
    job_id = job["id"]
    folder = WORK / "work" / job_id
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / ("source" + Path(job["relative_path"]).suffix)
    output = folder / "result.mp4"
    encoding = folder / "result.encoding.mp4"
    pull(job_id, int(job["size"]), source)
    source_probe = ffprobe(source)
    if output.exists():
        try:
            verify_output(job, source_probe, ffprobe(output))
        except (ValueError, subprocess.CalledProcessError):
            output.unlink()
    if not output.exists():
        encoding.unlink(missing_ok=True)
        command = command_for(job, source, encoding, source_probe)
        LOG.info("Converting %s with %s", job["relative_path"], job["profile"])
        with (folder / "ffmpeg.log").open("wb") as log_file:
            subprocess.run(command, stdout=log_file, stderr=subprocess.STDOUT, check=True)
        verify_output(job, source_probe, ffprobe(encoding))
        os.replace(encoding, output)
    output_probe = ffprobe(output)
    verify_output(job, source_probe, output_probe)
    upload(job_id, output)
    result = remote_json("complete", job_id, sha256(output), output.stat().st_size)
    LOG.info("Saved on server: %s", result["output"])
    source.unlink(missing_ok=True)
    output.unlink(missing_ok=True)


def get_mutex():
    if os.name != "nt":
        return None
    import ctypes
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\MovieCatcherConverterWorker")
    if ctypes.windll.kernel32.GetLastError() == 183:
        raise SystemExit("Another MovieCatcher converter is already running")
    return handle


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Poll once and exit")
    parser.add_argument("--job", help="Only process this job ID")
    arguments = parser.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(WORK / "worker.log", encoding="utf-8"),
                                  logging.StreamHandler(sys.stdout)])
    mutex = get_mutex()
    while True:
        try:
            jobs = remote_json("list")
            for job in jobs:
                if job.get("profile", "pending") == "pending":
                    continue
                if arguments.job and job["id"] != arguments.job:
                    continue
                try:
                    process(job)
                except Exception:
                    LOG.exception("Job %s failed and will retry later", job["id"])
        except Exception:
            LOG.exception("Server unavailable; queued work remains on server")
        if arguments.once or arguments.job:
            break
        time.sleep(30)
    if mutex is not None:
        import ctypes
        ctypes.windll.kernel32.CloseHandle(mutex)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
