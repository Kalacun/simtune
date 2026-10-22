#!/usr/bin/env python3
"""Tiny real fixed-work input-sensitive stage-05 correctness fixture."""

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    try:
        count = int(sys.argv[1])
    except ValueError:
        return 2
    if count < 0 or count > 100_000:
        return 2
    Path(sys.argv[2], "result.json").write_text(
        json.dumps({"science": {"value": sum(range(count))}}) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
