"""Fixed-work CPU plus buffered temporary-file I/O; not a disk benchmark."""

import math
import tempfile
from pathlib import Path


def main() -> None:
    total = sum(math.sqrt(9876.54321 + index % 17) for index in range(1_000_000))
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "blob.bin"
        data = b"y" * 1024 * 256
        path.write_bytes(data)
        assert path.read_bytes() == data
    print(f"checksum={total:.6f} bytes={len(data)}")


if __name__ == "__main__":
    main()
