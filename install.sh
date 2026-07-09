#!/usr/bin/env bash
# AIDE one-command installer.
#
#   Install:   curl -fsSL https://raw.githubusercontent.com/viktrum/aide/main/install.sh | bash
#   Uninstall: curl -fsSL https://raw.githubusercontent.com/viktrum/aide/main/install.sh | bash -s -- --uninstall
#
# What it does: clones (or updates) AIDE into ~/.aide, registers Claude Code
# hooks in ~/.claude/settings.json (backed up first), installs the /aide
# command, and runs a health check. Everything stays on your machine.
set -euo pipefail

REPO_URL="${AIDE_REPO_URL:-https://github.com/viktrum/aide}"
AIDE_HOME="${AIDE_HOME:-$HOME/.aide}"

say()  { printf '\033[1;32m[aide]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[aide]\033[0m %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
  Darwin|Linux) ;;
  *) fail "Unsupported OS: $(uname -s). AIDE currently supports macOS and Linux." ;;
esac

command -v git >/dev/null 2>&1 || fail "git is required. Install git and re-run."
command -v python3 >/dev/null 2>&1 || fail "python3 is required. Install Python 3.9+ and re-run."
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || fail "Python 3.9+ required (found $(python3 --version 2>&1))."

if [ "${1:-}" = "--uninstall" ]; then
  if [ -d "$AIDE_HOME" ]; then
    python3 "$AIDE_HOME/prototype/judge/install_hooks.py" --uninstall
    say "Hooks removed. Your data is untouched."
    say "To delete everything:  rm -rf '$AIDE_HOME' '$HOME/.claude-judge'"
  else
    say "Nothing installed at $AIDE_HOME."
  fi
  exit 0
fi

if [ -d "$AIDE_HOME/.git" ]; then
  say "Updating existing install at $AIDE_HOME"
  git -C "$AIDE_HOME" pull --ff-only --quiet || say "Update skipped (local changes?). Continuing with current version."
else
  say "Installing AIDE into $AIDE_HOME"
  git clone --depth 1 --quiet "$REPO_URL" "$AIDE_HOME"
fi

python3 "$AIDE_HOME/prototype/judge/install_hooks.py"

echo
python3 "$AIDE_HOME/prototype/judge/doctor.py" || true

echo
say "Done. Open a new Claude Code session — AIDE is active."
say "Check health any time:   /aide status   (or: python3 $AIDE_HOME/prototype/judge/doctor.py)"
say "Bypass for one prompt:   prefix it with *"
say "Uninstall:               curl -fsSL $REPO_URL/raw/main/install.sh | bash -s -- --uninstall"
