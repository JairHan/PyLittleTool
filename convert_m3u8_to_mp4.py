#!/usr/bin/env python3
"""Convert a local m3u8 playlist into an MP4 file.

The script validates local media segment references first, then asks ffmpeg to
remux the HLS stream into MP4 without re-encoding.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


def is_remote_uri(uri: str) -> bool:
    scheme = urlparse(uri).scheme.lower()
    return scheme in {"http", "https"}


def playlist_media_uris(playlist: Path) -> list[str]:
    uris: list[str] = []
    for raw_line in playlist.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        uris.append(line)
    return uris


def validate_local_segments(playlist: Path) -> None:
    missing: list[Path] = []
    for uri in playlist_media_uris(playlist):
        if is_remote_uri(uri) or uri.startswith("data:"):
            continue

        parsed = urlparse(uri)
        segment_path = Path(parsed.path)
        if not segment_path.is_absolute():
            segment_path = playlist.parent / segment_path

        if not segment_path.exists():
            missing.append(segment_path)

    if missing:
        preview = "\n".join(f"  - {path}" for path in missing[:10])
        extra = "" if len(missing) <= 10 else f"\n  ... and {len(missing) - 10} more"
        raise FileNotFoundError(f"Missing {len(missing)} media segment(s):\n{preview}{extra}")


def convert(playlist: Path, output: Path, overwrite: bool) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg was not found. Install it first, for example: brew install ffmpeg")

    if not playlist.exists():
        raise FileNotFoundError(f"Playlist not found: {playlist}")

    output.parent.mkdir(parents=True, exist_ok=True)
    validate_local_segments(playlist)

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "info",
        "-allowed_extensions",
        "ALL",
        "-protocol_whitelist",
        "file,crypto,data,pipe,http,https,tcp,tls",
        "-i",
        str(playlist),
        "-c",
        "copy",
        "-bsf:a",
        "aac_adtstoasc",
        "-movflags",
        "+faststart",
    ]
    command.append("-y" if overwrite else "-n")
    command.append(str(output))

    subprocess.run(command, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert a local m3u8 playlist to MP4.")
    parser.add_argument(
        "playlist",
        nargs="?",
        default="index.m3u8",
        help="Path to the .m3u8 playlist. Default: index.m3u8",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="output.mp4",
        help="Output MP4 path. Default: output.mp4",
    )
    parser.add_argument(
        "-y",
        "--overwrite",
        action="store_true",
        help="Overwrite the output file if it already exists.",
    )
    args = parser.parse_args()

    try:
        convert(Path(args.playlist).resolve(), Path(args.output).resolve(), args.overwrite)
    except subprocess.CalledProcessError as exc:
        print(f"ffmpeg failed with exit code {exc.returncode}", file=sys.stderr)
        return exc.returncode
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Done: {Path(args.output).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
