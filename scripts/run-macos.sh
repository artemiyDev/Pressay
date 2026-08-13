#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Pressay macOS launcher can only run on macOS." >&2
  exit 2
fi

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_root="${HOME}/Library/Application Support/Pressay/venv"
python_bin="${runtime_root}/bin/python"

if [[ ! -x "${python_bin}" ]]; then
  echo "Pressay runtime is missing. Run: bash scripts/setup-macos.sh" >&2
  exit 1
fi

export PYTHONPATH="${project_root}/src${PYTHONPATH:+:${PYTHONPATH}}"
exec "${python_bin}" -m pressay "$@"
