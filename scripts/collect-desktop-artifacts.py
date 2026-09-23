#!/usr/bin/env python3
"""Collect host-native Tauri bundles for CI uploads."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
EXTENSIONS = {".AppImage", ".deb", ".dmg", ".exe", ".msi", ".rpm"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True, help="Artifact platform/architecture label")
    parser.add_argument("--output", type=Path, default=ROOT / "release-artifacts")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)

    bundle_root = ROOT / "src-tauri" / "target" / "release" / "bundle"
    candidates = [
        path
        for path in bundle_root.rglob("*")
        if path.is_file() and (path.suffix in EXTENSIONS or path.name.endswith(".AppImage"))
    ]

    copied: list[Path] = []
    for source in candidates:
        if source.parent == output:
            copied.append(source)
            continue
        destination = output / f"{args.platform}-{source.name}"
        shutil.copy2(source, destination)
        copied.append(destination)

    if not copied:
        raise SystemExit(f"No desktop artifacts found under {bundle_root}")
    for path in sorted(set(copied)):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
