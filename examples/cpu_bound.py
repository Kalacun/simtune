"""Fixed-work CPU example. Compare equal iteration counts, not fixed durations."""

import argparse
import math


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=2_000_000)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be positive")
    total = sum(math.sqrt(12345.6789 + index % 17) for index in range(args.iterations))
    print(f"iterations={args.iterations} checksum={total:.6f}")


if __name__ == "__main__":
    main()
