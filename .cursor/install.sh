#!/usr/bin/env bash
# Idempotent Cloud Agent setup for Disk.Brain.
#
# Disk.Brain targets Apple Silicon macOS. Two of its dependencies are macOS
# only: ocrmac (Apple Vision OCR, via pyobjc) and the mlx runtime that mlx-lm
# imports for the reranker. Both are used through lazy, in-function imports, so
# the package installs and runs on Linux for everything except OCR and the MLX
# reranker. This script installs the full stack on a Linux Cloud Agent by
# scoping ocrmac to Darwin; mlx-lm itself is pure Python and installs fine.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# --- uv (pinned Python 3.12 toolchain used by the project) -------------------
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"

# --- pandoc --------------------------------------------------------------------
# Office/HTML/EPUB extraction falls through markitdown -> pandoc -> textutil.
# textutil is macOS only, so pandoc is the fallback engine on Linux.
if ! command -v pandoc >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq pandoc
fi

# --- Python environment ------------------------------------------------------
if [ ! -x .venv/bin/python ]; then
  uv venv --python 3.12
fi

# Skip the macOS-only OCR dependency on non-Darwin platforms. This only rewrites
# the resolver's marker for ocrmac; the real dependency stays intact for macOS.
OVERRIDE_FILE="$(mktemp)"
printf "ocrmac ; platform_system == 'Darwin'\n" > "$OVERRIDE_FILE"
uv pip install -e ".[dev]" --override "$OVERRIDE_FILE"
rm -f "$OVERRIDE_FILE"

echo "Disk.Brain environment ready. Activate with: source .venv/bin/activate"
