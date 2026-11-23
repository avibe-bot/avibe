"""POSIX contract: an undrained diagnostics pipe cannot prevent fatal exit."""
import json
import os
import subprocess
import sys


def check(binary):
    reader, writer = os.pipe()
    try:
        os.set_blocking(writer, False)
        for block in (bytes(4096), b"x"):
            try:
                while True:
                    os.write(writer, block)
            except BlockingIOError:
                pass
        os.set_blocking(writer, True)
        # Keep the read end open without consuming anything. EOF on stdin
        # takes the start_required fatal path without starting networking.
        result = subprocess.run([binary], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=writer, timeout=5, check=False)
        assert result.returncode == 1, result.returncode
        assert json.loads(result.stdout) == {"type": "fatal", "error": "start_required"}
    finally:
        os.close(writer)
        os.close(reader)


if __name__ == "__main__":
    check(sys.argv[1])
