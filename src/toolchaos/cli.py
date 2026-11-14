"""``toolchaos inspect cassette.jsonl``: show what a cassette contains."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="toolchaos")
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect", help="summarise the calls recorded in a cassette")
    inspect.add_argument("cassette", type=Path)
    args = parser.parse_args(argv)

    entries = [json.loads(line) for line in args.cassette.read_text(encoding="utf-8").splitlines() if line.strip()]
    counts = Counter(e["name"] for e in entries)
    errors = Counter(e["name"] for e in entries if "error" in e)
    print(f"{args.cassette}: {len(entries)} calls")
    for name, n in counts.items():
        print(f"  {name:<24} {n:>4} calls" + (f", {errors[name]} recorded errors" if errors[name] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
