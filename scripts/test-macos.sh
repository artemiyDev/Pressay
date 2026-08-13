#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${HOME}/Library/Application Support/Pressay/venv/bin/python"
if [[ ! -x "${python_bin}" ]]; then
  echo "Pressay runtime is missing. Run: bash scripts/setup-macos.sh --skip-model" >&2
  exit 1
fi
"${python_bin}" -m pip install "pytest>=8.3,<10" "pytest-cov>=6,<8"
export PYTHONPATH="${project_root}/src${PYTHONPATH:+:${PYTHONPATH}}"
exec "${python_bin}" -B -m pytest -q "$@"
