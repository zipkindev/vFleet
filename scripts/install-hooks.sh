#!/usr/bin/env bash
# Install vFleet git hooks into .git/hooks/.
# Run once after cloning:  ./scripts/install-hooks.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOKS_DIR="$REPO_ROOT/.git/hooks"
SCRIPTS_DIR="$REPO_ROOT/scripts"

if [[ ! -d "$HOOKS_DIR" ]]; then
  echo "ERROR: $HOOKS_DIR does not exist. Is this a git repo?" >&2
  exit 1
fi

install_hook() {
  local name="$1"
  local src="$SCRIPTS_DIR/hooks/$name"
  local dst="$HOOKS_DIR/$name"

  if [[ ! -f "$src" ]]; then
    echo "[skip] $src not found" >&2
    return
  fi

  cp "$src" "$dst"
  chmod +x "$dst"
  echo "  installed: .git/hooks/$name"
}

# Copy the hook sources from scripts/hooks/ into .git/hooks/
mkdir -p "$SCRIPTS_DIR/hooks"

# Inline-generate the canonical hook sources so install-hooks.sh is
# the single file a developer needs after a fresh clone.

cat > "$SCRIPTS_DIR/hooks/pre-commit" << 'HOOK'
#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="$(git rev-parse --show-toplevel)"
BUMP="${VFLEET_BUMP:-patch}"
SCRIPT="$REPO_ROOT/scripts/bump-version.py"
if [[ ! -f "$SCRIPT" ]]; then
  echo "[pre-commit] scripts/bump-version.py not found — skipping" >&2
  exit 0
fi
python3 "$SCRIPT" --bump "$BUMP"
HOOK

cat > "$SCRIPTS_DIR/hooks/commit-msg" << 'HOOK'
#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="$(git rev-parse --show-toplevel)"
VERSION_FILE="$REPO_ROOT/VERSION"
COMMIT_MSG_FILE="$1"
[[ ! -f "$VERSION_FILE" ]] && exit 0
VERSION="$(cat "$VERSION_FILE" | tr -d '[:space:]')"
[[ -z "$VERSION" ]] && exit 0
if ! grep -q "^Version:" "$COMMIT_MSG_FILE" 2>/dev/null; then
  printf "\nVersion: %s\n" "$VERSION" >> "$COMMIT_MSG_FILE"
fi
HOOK

chmod +x "$SCRIPTS_DIR/hooks/pre-commit" "$SCRIPTS_DIR/hooks/commit-msg"

install_hook "pre-commit"
install_hook "commit-msg"

echo ""
echo "vFleet git hooks installed."
echo ""
echo "Every commit will:"
echo "  1. Auto-bump the patch version (or VFLEET_BUMP=minor|major)"
echo "  2. Sync VERSION, version.ts, package.json, main.py"
echo "  3. Prepend a dated stub to CHANGELOG.md"
echo "  4. Append 'Version: X.Y.Z' trailer to the commit message"
echo ""
echo "Skip for a specific commit:  git commit --no-verify"
