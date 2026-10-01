"""Read-only inventory of video subtitle streams and adjacent subtitle files."""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

VIDEO_SUFFIXES = {".mkv", ".mp4", ".m4v", ".mov", ".ts", ".m2ts", ".mts", ".avi", ".webm"}
SUBTITLE_SUFFIXES = {".srt", ".ass", ".ssa", ".sub", ".vtt"}
ENGLISH_TAGS = {"en", "eng", "english", "en-us", "en_us"}
PERSIAN_TAGS = {"fa", "fas", "per", "farsi", "persian"}


def inspect(video: Path) -> dict:
    command = [
        "ffprobe", "-v", "error", "-select_streams", "s",
        "-show_entries", "stream=index,codec_name:stream_tags=language,title:stream_disposition=default,forced,hearing_impaired",
        "-of", "json", str(video),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=40)
    streams = json.loads(result.stdout).get("streams", []) if result.returncode == 0 else []
    sidecars = [
        {"path": str(path), "size": path.stat().st_size}
        for path in video.parent.iterdir()
        if path.is_file() and path.suffix.lower() in SUBTITLE_SUFFIXES
        and (path.name == f"{video.stem}{path.suffix}" or path.name.startswith(video.stem + "."))
    ]
    english_embedded = [stream for stream in streams if str(stream.get("tags", {}).get("language", "")).lower() in ENGLISH_TAGS]
    persian_embedded = [stream for stream in streams if str(stream.get("tags", {}).get("language", "")).lower() in PERSIAN_TAGS]
    english_sidecars = [
        sidecar for sidecar in sidecars
        if any(tag in Path(sidecar["path"]).stem[len(video.stem):].lower().split(".") for tag in ENGLISH_TAGS)
    ]
    return {
        "path": str(video), "size": video.stat().st_size, "streams": streams,
        "sidecars": sidecars, "english_embedded": len(english_embedded),
        "persian_embedded": len(persian_embedded), "english_sidecars": len(english_sidecars),
        "error": result.stderr[-300:] if result.returncode else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    targets = sorted(
        (path, root.name) for root in args.roots for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES and not path.name.startswith("._")
    )
    with ThreadPoolExecutor(max_workers=4) as executor:
        items = list(executor.map(inspect, (path for path, _ in targets)))
    counts = Counter()
    by_category: dict[str, Counter] = {}
    for item, (_, category) in zip(items, targets):
        item["category"] = category
        group = by_category.setdefault(category, Counter())
        counts[category] += 1
        group["total"] += 1
        counts["with_any_english"] += int(item["english_embedded"] + item["english_sidecars"] > 0)
        group["with_any_english"] += int(item["english_embedded"] + item["english_sidecars"] > 0)
        counts["without_english"] += int(item["english_embedded"] + item["english_sidecars"] == 0)
        group["without_english"] += int(item["english_embedded"] + item["english_sidecars"] == 0)
        counts["multiple_english"] += int(item["english_embedded"] + item["english_sidecars"] > 1)
        group["multiple_english"] += int(item["english_embedded"] + item["english_sidecars"] > 1)
        counts["with_persian_embedded"] += int(item["persian_embedded"] > 0)
        group["with_persian_embedded"] += int(item["persian_embedded"] > 0)
        counts["probe_errors"] += int(item["error"] is not None)
        group["probe_errors"] += int(item["error"] is not None)
    report = {"counts": counts, "by_category": by_category, "items": items}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"counts": counts, "by_category": by_category}, ensure_ascii=False))


if __name__ == "__main__":
    main()
