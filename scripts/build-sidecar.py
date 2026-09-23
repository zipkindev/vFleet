#!/usr/bin/env python3
"""Build the Python desktop backend and name it for Tauri's target triple."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def host_triple() -> str:
    try:
        result = subprocess.run(
            ["rustc", "--print", "host-tuple"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit("rustc is required to determine the Tauri sidecar target triple") from exc
    triple = result.stdout.strip()
    if not triple:
        raise SystemExit("rustc returned an empty host target triple")
    return triple


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-triple", help="Override rustc's host target triple")
    parser.add_argument("--skip-build", action="store_true", help="Only copy an existing PyInstaller result")
    args = parser.parse_args()

    if not (ROOT / "frontend" / "dist" / "index.html").is_file():
        raise SystemExit("frontend/dist is missing; run npm run build in frontend first")

    extension = ".exe" if sys.platform == "win32" else ""
    source = ROOT / "dist" / f"vfleet-backend{extension}"
    if not args.skip_build:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                "--clean",
                "--noconfirm",
                str(ROOT / "packaging" / "vfleet-backend.spec"),
            ],
            cwd=ROOT,
            check=True,
        )
    if not source.is_file():
        raise SystemExit(f"PyInstaller did not create {source}")

    triple = args.target_triple or host_triple()
    destination_dir = ROOT / "src-tauri" / "binaries"
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"vfleet-backend-{triple}{extension}"
    shutil.copy2(source, destination)
    destination.chmod(destination.stat().st_mode | 0o111)
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
