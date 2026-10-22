#!/usr/bin/env python3
"""Harmless fixed-work stage-04 fixture."""

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    _declared_input, output_directory = sys.argv[1:]
    Path(output_directory, "result.json").write_text(
        json.dumps({"science": {"value": sum(range(4))}}) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
