#!/usr/bin/env python3
"""Run isolated unit processes and a temporary local HTTP smoke server."""

from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request
import socket

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    results = []
    env = dict(
        os.environ,
        CDT_DRY_RUN="1",
        CDT_MOCK_OP_DELAY="0",
        CDT_BASE_PATH="",
        CDT_SECURE_COOKIES="0",
        CDT_PROBE_CONFIG="",
    )
    with tempfile.TemporaryDirectory(prefix="cdt-checks-") as temp:
        for file in sorted((ROOT / "scripts").glob("test_*.py")):
            result = subprocess.run(
                [sys.executable, str(file)],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            results.append({"script": file.name, "passed": result.returncode == 0})
            print(file.name, "PASS" if result.returncode == 0 else "FAIL", flush=True)
            if result.returncode:
                print((result.stdout + result.stderr)[-6000:])
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env.update(
            CDT_DATA_DIR=temp, CDT_LISTEN_HOST="127.0.0.1", CDT_LISTEN_PORT=str(port)
        )
        with open(Path(temp) / "server.log", "w") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "app.main"],
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=log,
            )
            try:
                ready = False
                for _ in range(80):
                    if process.poll() is not None:
                        break
                    try:
                        with urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/api/ready", timeout=1
                        ) as response:
                            ready = response.status == 200
                        if ready:
                            break
                    except Exception:
                        time.sleep(0.1)
                if not ready:
                    log.flush()
                    print("Local smoke service did not become ready")
                    print((Path(temp) / "server.log").read_text()[-4000:])
                    return 1
                result = subprocess.run(
                    [
                        sys.executable,
                        "scripts/smoke_test.py",
                        f"http://127.0.0.1:{port}",
                    ],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                results.append(
                    {"script": "smoke_test.py", "passed": result.returncode == 0}
                )
                print("HTTP smoke", "PASS" if result.returncode == 0 else "FAIL")
                if result.returncode:
                    print(
                        "\n".join(
                            line
                            for line in (result.stdout + result.stderr).splitlines()
                            if "❌" in line or "Error" in line
                        )
                    )
            finally:
                process.terminate()
                try:
                    process.wait(timeout=75)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    print(json.dumps(results, indent=2))
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
