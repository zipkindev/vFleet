#!/usr/bin/env python3
"""
vFleet version bumper — called by the pre-commit git hook.

Reads VERSION, increments the patch segment, then syncs the new version
into every file that carries a version string and prepends a dated stub
entry to CHANGELOG.md.

Usage (normally called by the hook, but can be run manually):
    python3 scripts/bump-version.py [--dry-run] [--bump minor|major]

Flags
-----
--dry-run   Print what would change but write nothing.
--bump      Which segment to increment (default: patch).
            Pass "minor" or "major" from the command line when releasing
            a feature or breaking-change version.

Exit codes
----------
0  Success (or --dry-run preview).
1  VERSION file missing or malformed.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Files that carry the version string and how to update them
# ---------------------------------------------------------------------------

def _replace_in_file(path: Path, pattern: str, replacement: str, dry_run: bool) -> bool:
    """Return True if the file was (or would be) changed."""
    text = path.read_text(encoding="utf-8")
    new_text, n = re.subn(pattern, replacement, text)
    if n == 0:
        print(f"  [warn] no match for {pattern!r} in {path.relative_to(REPO_ROOT)}", file=sys.stderr)
        return False
    if text == new_text:
        return False
    if not dry_run:
        path.write_text(new_text, encoding="utf-8")
    return True


def sync_version_files(old: str, new: str, dry_run: bool) -> list[Path]:
    """Update every file that carries the version and return the changed paths."""
    changed: list[Path] = []

    targets: list[tuple[Path, str, str]] = [
        # (file, search pattern, replacement)
        (
            REPO_ROOT / "VERSION",
            re.escape(old),
            new,
        ),
        (
            REPO_ROOT / "frontend" / "src" / "version.ts",
            r'(APP_VERSION\s*=\s*")[^"]+(")',
            rf'\g<1>{new}\2',
        ),
        (
            REPO_ROOT / "frontend" / "package.json",
            r'("version"\s*:\s*")[^"]+(")',
            rf'\g<1>{new}\2',
        ),
        (
            REPO_ROOT / "backend" / "app" / "main.py",
            r'(APP_VERSION\s*=\s*")[^"]+(")',
            rf'\g<1>{new}\2',
        ),
        (
            REPO_ROOT / "package.json",
            r'("version"\s*:\s*")[^"]+(")',
            rf'\g<1>{new}\2',
        ),
        (
            REPO_ROOT / "pyproject.toml",
            r'(?m)^(version\s*=\s*")[^"]+(")',
            rf'\g<1>{new}\2',
        ),
        (
            REPO_ROOT / "src-tauri" / "Cargo.toml",
            r'(?m)^(version\s*=\s*")[^"]+(")',
            rf'\g<1>{new}\2',
        ),
        (
            REPO_ROOT / "src-tauri" / "Cargo.lock",
            r'(?ms)(\[\[package\]\]\s+name\s*=\s*"vfleet"\s+version\s*=\s*")[^"]+(")',
            rf'\g<1>{new}\2',
        ),
        (
            REPO_ROOT / "src-tauri" / "tauri.conf.json",
            r'("version"\s*:\s*")[^"]+(")',
            rf'\g<1>{new}\2',
        ),
    ]

    for path, pattern, replacement in targets:
        if not path.exists():
            print(f"  [warn] {path.relative_to(REPO_ROOT)} not found — skipping", file=sys.stderr)
            continue
        label = "would update" if dry_run else "updated"
        if _replace_in_file(path, pattern, replacement, dry_run):
            print(f"  {label}: {path.relative_to(REPO_ROOT)}")
            changed.append(path)

    for path in (REPO_ROOT / "frontend" / "package-lock.json", REPO_ROOT / "package-lock.json"):
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        before = json.dumps(payload, sort_keys=True)
        payload["version"] = new
        root_package = payload.get("packages", {}).get("")
        if isinstance(root_package, dict):
            root_package["version"] = new
        after = json.dumps(payload, sort_keys=True)
        if before != after:
            label = "would update" if dry_run else "updated"
            if not dry_run:
                path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            print(f"  {label}: {path.relative_to(REPO_ROOT)}")
            changed.append(path)

    return changed


# ---------------------------------------------------------------------------
# CHANGELOG helpers
# ---------------------------------------------------------------------------

CHANGELOG = REPO_ROOT / "CHANGELOG.md"
_HEADER_RE = re.compile(r"^(# Changelog.*?\n)(.*)", re.DOTALL)


def _collect_staged_summary() -> str:
    """Return a one-line summary of staged changes for the changelog stub."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--stat", "--no-color"],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        lines = [l for l in result.stdout.splitlines() if l.strip() and not l.strip().startswith("Bin")]
        # Last line is the summary ("3 files changed, 42 insertions…")
        summary_line = lines[-1].strip() if lines else ""
        # Collect changed file names (skip the summary line)
        file_lines = lines[:-1] if len(lines) > 1 else []
        file_names = [re.match(r"\s*(.+?)\s*\|", l) for l in file_lines]
        names = [m.group(1).strip() for m in file_names if m][:6]
        if names:
            extra = f" · {', '.join(names)}"
            if len(names) == 6:
                extra += ", …"
        else:
            extra = ""
        return f"{summary_line}{extra}" if summary_line else "staged changes"
    except Exception:
        return "staged changes"


def _collect_commit_subject() -> str:
    """Return the first line of the commit message being prepared, if available."""
    msg_file = REPO_ROOT / ".git" / "COMMIT_EDITMSG"
    if msg_file.exists():
        for line in msg_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    return ""


def prepend_changelog(new_version: str, dry_run: bool) -> bool:
    """Prepend a dated stub section for new_version into CHANGELOG.md."""
    if not CHANGELOG.exists():
        print(f"  [warn] CHANGELOG.md not found — skipping", file=sys.stderr)
        return False

    today = datetime.date.today().isoformat()
    subject = _collect_commit_subject()
    staged = _collect_staged_summary()

    entry_lines = [f"\n## [{new_version}] — {today}\n"]
    if subject:
        entry_lines.append(f"\n{subject}\n")
    entry_lines.append(f"\n- {staged}\n")

    entry = "".join(entry_lines)

    text = CHANGELOG.read_text(encoding="utf-8")
    m = _HEADER_RE.match(text)
    if m:
        new_text = m.group(1) + entry + m.group(2)
    else:
        new_text = entry + "\n" + text

    if dry_run:
        print(f"  would prepend to: CHANGELOG.md")
        print("  --- stub ---")
        print(entry.rstrip())
        print("  --- end ---")
        return True

    CHANGELOG.write_text(new_text, encoding="utf-8")
    print(f"  updated: CHANGELOG.md")
    return True


# ---------------------------------------------------------------------------
# Version arithmetic
# ---------------------------------------------------------------------------

def parse_version(raw: str) -> tuple[int, int, int]:
    raw = raw.strip()
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", raw)
    if not m:
        raise ValueError(f"Cannot parse version {raw!r} — expected MAJOR.MINOR.PATCH")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def bump(version: tuple[int, int, int], segment: str) -> tuple[int, int, int]:
    major, minor, patch = version
    if segment == "major":
        return (major + 1, 0, 0)
    if segment == "minor":
        return (major, minor + 1, 0)
    return (major, minor, patch + 1)


def fmt(version: tuple[int, int, int]) -> str:
    return "%d.%d.%d" % version


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Bump vFleet version")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--bump", choices=["patch", "minor", "major"], default="patch")
    args = parser.parse_args()

    version_file = REPO_ROOT / "VERSION"
    if not version_file.exists():
        print("ERROR: VERSION file not found", file=sys.stderr)
        return 1

    try:
        current = parse_version(version_file.read_text())
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    new = bump(current, args.bump)
    old_str = fmt(current)
    new_str = fmt(new)

    if args.dry_run:
        print(f"[dry-run] {old_str} → {new_str} ({args.bump} bump)")
    else:
        print(f"bumping {old_str} → {new_str}")

    changed = sync_version_files(old_str, new_str, args.dry_run)
    prepend_changelog(new_str, args.dry_run)

    if not args.dry_run and changed:
        # Stage everything that was changed so the commit includes the bumped files
        subprocess.run(
            ["git", "add"] + [str(p) for p in changed] + [str(CHANGELOG)],
            cwd=REPO_ROOT,
            check=False,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
