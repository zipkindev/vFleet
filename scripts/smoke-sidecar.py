#!/usr/bin/env python3
"""Start a packaged sidecar, verify its HTTP surface, and stop it cleanly."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _default_binary() -> Path:
    try:
        triple = subprocess.run(
            ["rustc", "--print", "host-tuple"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit("pass a sidecar path or install rustc so its target triple can be detected") from exc
    extension = ".exe" if sys.platform == "win32" else ""
    return ROOT / "src-tauri" / "binaries" / f"vfleet-backend-{triple}{extension}"


def _reader(stream, lines: list[str]) -> None:
    for line in iter(stream.readline, ""):
        lines.append(line.rstrip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", nargs="?", type=Path)
    parser.add_argument("--timeout", type=float, default=45)
    args = parser.parse_args()
    binary = (args.binary or _default_binary()).resolve()
    if not binary.is_file():
        raise SystemExit(f"Sidecar does not exist: {binary}")

    with tempfile.TemporaryDirectory(prefix="vfleet-sidecar-smoke-") as temp:
        env = os.environ.copy()
        env.update(
            {
                "APP_MODE": "demo",
                "DATA_DIR": temp,
                "VFLEET_MASTER_KEY_FILE": str(Path(temp) / "credential.key"),
                "VCENTER_HOST": "",
                "VCENTER_USER": "",
                "VCENTER_PASSWORD": "",
            }
        )
        process = subprocess.Popen(
            [str(binary), "--port", "0"],
            cwd=ROOT,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        stdout: list[str] = []
        stderr: list[str] = []
        threading.Thread(target=_reader, args=(process.stdout, stdout), daemon=True).start()
        threading.Thread(target=_reader, args=(process.stderr, stderr), daemon=True).start()
        try:
            deadline = time.monotonic() + args.timeout
            url = ""
            while time.monotonic() < deadline and process.poll() is None:
                ready = next((line for line in stdout if line.startswith("VFLEET_READY ")), "")
                if ready:
                    url = ready.removeprefix("VFLEET_READY ").strip()
                    break
                time.sleep(0.05)
            if not url:
                raise RuntimeError("sidecar did not announce readiness")

            with urllib.request.urlopen(url + "/api/health", timeout=10) as response:
                health = json.load(response)
            with urllib.request.urlopen(url + "/api/version", timeout=10) as response:
                version = json.load(response)
            with urllib.request.urlopen(url + "/api/changelog", timeout=10) as response:
                changelog = json.load(response)
            with urllib.request.urlopen(url + "/", timeout=10) as response:
                html = response.read().decode("utf-8")
            if health.get("ok") is not True or health.get("mode") != "demo":
                raise RuntimeError(f"unexpected health response: {health}")
            expected = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
            if version.get("version") != expected:
                raise RuntimeError(f"sidecar version {version!r} does not match {expected}")
            if "# Changelog" not in str(changelog.get("content")):
                raise RuntimeError("packaged changelog is missing")
            if "<title>vFleet" not in html or '<div id="root"></div>' not in html:
                raise RuntimeError("packaged frontend is missing")

            assert process.stdin is not None
            process.stdin.write("SHUTDOWN\n")
            process.stdin.flush()
            process.wait(timeout=15)
            if process.returncode != 0:
                raise RuntimeError(f"sidecar exited with {process.returncode}")
        except Exception as exc:
            process.kill()
            process.wait(timeout=10)
            details = "\n".join((stdout + stderr)[-40:])
            raise SystemExit(f"{exc}\n{details}") from exc

    print(f"Sidecar smoke passed: {binary.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
