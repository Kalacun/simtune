"""Buffered file I/O followed by a short sleep; a classification demo only."""

import os
import tempfile
import time


def main():
    data = b"x" * 1024 * 128
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "blob.bin")
        with open(path, "wb") as handle:
            for _ in range(64):
                handle.write(data)
        with open(path, "rb") as handle:
            while handle.read(1024 * 64):
                pass
    time.sleep(0.2)
    print("done")


if __name__ == "__main__":
    main()
