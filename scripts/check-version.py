#!/usr/bin/env python3
"""Fail when release metadata is not synchronized with VERSION."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
EXPECTED = (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def json_version(path: str) -> str:
    return str(json.loads((ROOT / path).read_text(encoding="utf-8"))["version"])


def lockfile_version(path: str) -> str:
    payload = json.loads((ROOT / path).read_text(encoding="utf-8"))
    version = str(payload["version"])
    package_version = str(payload.get("packages", {}).get("", {}).get("version", ""))
    if package_version != version:
        raise RuntimeError(
            f"{path}: top-level version {version!r} does not match packages[''].version {package_version!r}"
        )
    return version


def pattern_version(path: str, pattern: str) -> str:
    text = (ROOT / path).read_text(encoding="utf-8")
    match = re.search(pattern, text)
    if not match:
        raise RuntimeError(f"could not locate a version in {path}")
    return match.group(1)


def main() -> int:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", EXPECTED):
        print(f"VERSION is not a release semantic version: {EXPECTED!r}", file=sys.stderr)
        return 1
    versions = {
        "backend/app/main.py": pattern_version("backend/app/main.py", r'APP_VERSION\s*=\s*"([^"]+)"'),
        "frontend/src/version.ts": pattern_version("frontend/src/version.ts", r'APP_VERSION\s*=\s*"([^"]+)"'),
        "frontend/package.json": json_version("frontend/package.json"),
        "frontend/package-lock.json": lockfile_version("frontend/package-lock.json"),
        "package.json": json_version("package.json"),
        "pyproject.toml": pattern_version("pyproject.toml", r'(?m)^version\s*=\s*"([^"]+)"'),
        "src-tauri/Cargo.toml": pattern_version("src-tauri/Cargo.toml", r'(?m)^version\s*=\s*"([^"]+)"'),
        "src-tauri/Cargo.lock": pattern_version(
            "src-tauri/Cargo.lock",
            r'(?ms)\[\[package\]\]\s+name\s*=\s*"vfleet"\s+version\s*=\s*"([^"]+)"',
        ),
        "src-tauri/tauri.conf.json": json_version("src-tauri/tauri.conf.json"),
    }
    if (ROOT / "package-lock.json").exists():
        versions["package-lock.json"] = lockfile_version("package-lock.json")
    mismatches = {path: version for path, version in versions.items() if version != EXPECTED}
    if mismatches:
        for path, version in mismatches.items():
            print(f"{path}: {version} (expected {EXPECTED})", file=sys.stderr)
        return 1
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if f"## [{EXPECTED}]" not in changelog:
        print(f"CHANGELOG.md has no entry for {EXPECTED}", file=sys.stderr)
        return 1
    print(f"All release metadata matches {EXPECTED}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
