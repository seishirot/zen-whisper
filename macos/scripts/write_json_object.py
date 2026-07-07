from __future__ import annotations

import argparse
import json
from pathlib import Path


def _entries(parser: argparse.ArgumentParser, values: list[list[str]], kind: str):
    for entry in values:
        if len(entry) != 2:
            parser.error(f"--{kind} requires KEY VALUE")
        yield entry[0], entry[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Write a small JSON object safely.")
    parser.add_argument("output")
    parser.add_argument("--string", nargs=2, action="append", default=[], metavar=("KEY", "VALUE"))
    parser.add_argument("--int", nargs=2, action="append", default=[], metavar=("KEY", "VALUE"))
    args = parser.parse_args()

    data: dict[str, object] = {}
    for key, value in _entries(parser, args.string, "string"):
        if key in data:
            parser.error(f"duplicate key: {key}")
        data[key] = value
    for key, value in _entries(parser, args.int, "int"):
        if key in data:
            parser.error(f"duplicate key: {key}")
        try:
            data[key] = int(value)
        except ValueError:
            parser.error(f"--int {key} must be an integer: {value}")

    Path(args.output).write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
