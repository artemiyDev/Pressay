#!/usr/bin/env bash
set -euo pipefail

model="small"
python_command="python3.11"
skip_model=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model) model="${2:?--model requires a value}"; shift 2 ;;
    --python) python_command="${2:?--python requires a value}"; shift 2 ;;
    --skip-model) skip_model=1; shift ;;
    -h|--help)
      echo "Usage: bash scripts/setup-macos.sh [--model small] [--python python3.11] [--skip-model]"
      exit 0
      ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Pressay macOS setup can only run on macOS." >&2
  exit 2
fi
if ! command -v "${python_command}" >/dev/null 2>&1; then
  echo "Python 3.11 was not found. Install it from python.org or Homebrew, then retry." >&2
  exit 1
fi

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
data_root="${HOME}/Library/Application Support/Pressay"
venv_root="${data_root}/venv"
python_bin="${venv_root}/bin/python"

mkdir -p "${data_root}"
if [[ ! -x "${python_bin}" ]]; then
  "${python_command}" -m venv "${venv_root}"
fi

"${python_bin}" -m pip install --upgrade pip setuptools wheel
"${python_bin}" -m pip install "${project_root}[macos]"

export PYTHONPATH="${project_root}/src${PYTHONPATH:+:${PYTHONPATH}}"
if [[ ! -f "${data_root}/config.json" ]]; then
  PRESSAY_SETUP_MODEL="${model}" "${python_bin}" - <<'PY'
import os
from pressay.config import AppConfig

AppConfig(model=os.environ["PRESSAY_SETUP_MODEL"], resource_mode="balanced").save()
PY
fi

if [[ "${skip_model}" -eq 0 ]]; then
  "${python_bin}" -m pressay.model_setup --model "${model}" --device cpu
fi

echo "Pressay macOS runtime is ready at ${venv_root}"
