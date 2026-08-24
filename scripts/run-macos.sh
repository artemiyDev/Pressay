#!/usr/bin/env bash
set -euo pipefail

launcher_root="${HOME}/Library/Application Support/Pressay"
launcher_log="${launcher_root}/launcher.log"
launcher_mode="foreground"
for argument in "$@"; do
  if [[ "${argument}" == "--background" ]]; then
    launcher_mode="background"
    break
  fi
done

write_launcher_failure() {
  local message="${1//$'\r'/ }"
  message="${message//$'\n'/ }"
  local maximum_bytes=204800
  local retained_bytes=153600
  local temporary_log="${launcher_log}.tmp.$$"

  mkdir -p "${launcher_root}" 2>/dev/null || return 0
  if [[ -f "${launcher_log}" ]] &&
    [[ "$(stat -f '%z' "${launcher_log}" 2>/dev/null || printf '0')" -gt "${maximum_bytes}" ]]; then
    if tail -c "${retained_bytes}" "${launcher_log}" >"${temporary_log}" 2>/dev/null; then
      mv -f "${temporary_log}" "${launcher_log}" 2>/dev/null || true
    else
      rm -f "${temporary_log}" 2>/dev/null || true
    fi
  fi
  printf '%s mode=%s error=%s\n' \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    "${launcher_mode}" \
    "${message}" >>"${launcher_log}" 2>/dev/null || true
}

fail_launcher() {
  local exit_code="$1"
  shift
  local message="$*"
  write_launcher_failure "${message}"
  printf '%s\n' "${message}" >&2
  exit "${exit_code}"
}

if [[ "$(uname -s)" != "Darwin" ]]; then
  fail_launcher 2 "Pressay macOS launcher can only run on macOS."
fi

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_root="${launcher_root}/venv"
python_bin="${runtime_root}/bin/python"

if [[ ! -x "${python_bin}" ]]; then
  fail_launcher 1 "Pressay runtime is missing. Run: bash scripts/setup-macos.sh"
fi

export PYTHONPATH="${project_root}/src${PYTHONPATH:+:${PYTHONPATH}}"
exec "${python_bin}" -m pressay "$@"
