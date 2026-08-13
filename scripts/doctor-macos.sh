#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${HOME}/Library/Application Support/Pressay/venv/bin/python"
if [[ ! -x "${python_bin}" ]]; then
  echo "Pressay runtime is missing. Run: bash scripts/setup-macos.sh" >&2
  exit 1
fi
export PYTHONPATH="${project_root}/src${PYTHONPATH:+:${PYTHONPATH}}"
exec "${python_bin}" -m pressay.doctor --model small "$@"
