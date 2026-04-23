#!/usr/bin/env bash
# bcdl.sh — prompt for a Bandcamp URL, download via poetry-managed bcdl.py,
# save into ~/Downloads.
#
# On first run, this will create the poetry env and install deps.

set -euo pipefail

# Resolve this script's directory so it works no matter where it's invoked from.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
cd "$SCRIPT_DIR"

# Where to save downloads. Honors $XDG_DOWNLOAD_DIR on Linux if set,
# otherwise falls back to ~/Downloads.
DOWNLOADS_DIR="./downloads"

# Sanity check: poetry installed?
if ! command -v poetry &> /dev/null; then
    echo "error: poetry is not installed or not on PATH." >&2
    echo "install it from https://python-poetry.org/docs/#installation" >&2
    exit 1
fi

# Sanity check: the python script and pyproject are next to us.
if [[ ! -f "$SCRIPT_DIR/bcdl.py" || ! -f "$SCRIPT_DIR/pyproject.toml" ]]; then
    echo "error: bcdl.py and pyproject.toml must sit next to this script." >&2
    echo "(looked in: $SCRIPT_DIR)" >&2
    exit 1
fi

# Install deps into poetry env on first run. --no-root because this isn't a
# real package. `poetry install` is idempotent — it's a no-op after the first run.
if [[ ! -d "$(poetry env info --path 2>/dev/null || true)" ]]; then
    echo "First run: installing dependencies into poetry env..."
    poetry install --no-root --quiet
fi

# Prompt for URL. Accept as positional arg too for scripting convenience.
if [[ $# -ge 1 ]]; then
    url="$1"
else
    read -rp "Bandcamp URL: " url
fi

if [[ -z "${url:-}" ]]; then
    echo "error: no URL provided." >&2
    exit 1
fi

echo "Saving to: $DOWNLOADS_DIR"
echo
exec poetry run python bcdl.py "$url" -o "$DOWNLOADS_DIR"