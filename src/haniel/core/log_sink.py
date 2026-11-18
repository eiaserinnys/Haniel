"""Relay one service output stream into its service log file.

Haniel starts one relay per stream for services declared as overlap targets
in their repository manifest. The relay owns no lifecycle relationship with
the service beyond the pipe: it appends lines with the same head LogCapture
uses and exits on EOF, so a Haniel replacement never cuts the log stream.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import BinaryIO

from .logs import format_log_line


def relay(source: str, log_path: Path, stream: BinaryIO) -> None:
    """Append every line read from the stream until EOF."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8", buffering=1) as log_file:
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", errors="replace")
            formatted = format_log_line(line, source)
            if formatted is not None:
                log_file.write(formatted + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Append a service stream to its Haniel log file until EOF."
    )
    parser.add_argument("--service", required=True, help="Haniel service name")
    parser.add_argument(
        "--source", required=True, choices=("stdout", "stderr"), help="Stream name"
    )
    parser.add_argument(
        "--log-file", required=True, help="Service log file to append to"
    )
    args = parser.parse_args(argv)
    try:
        relay(args.source, Path(args.log_file), sys.stdin.buffer)
    except OSError as error:
        print(f"log_sink for {args.service} failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
