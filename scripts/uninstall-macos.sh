#!/usr/bin/env bash
set -euo pipefail

remove_runtime=0
remove_user_data=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --remove-runtime) remove_runtime=1; shift ;;
    --remove-user-data) remove_user_data=1; shift ;;
    -h|--help)
      echo "Usage: bash scripts/uninstall-macos.sh [--remove-runtime] [--remove-user-data]"
      exit 0
      ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Pressay macOS uninstaller can only run on macOS." >&2
  exit 2
fi

app_bundle="${HOME}/Applications/Pressay.app"
agent_path="${HOME}/Library/LaunchAgents/dev.pressay.app.plist"
data_root="${HOME}/Library/Application Support/Pressay"
runtime_root="${data_root}/venv"

launchctl bootout "gui/${UID}" "${agent_path}" >/dev/null 2>&1 || true
[[ ! -e "${agent_path}" ]] || rm -f -- "${agent_path}"
[[ ! -e "${app_bundle}" ]] || rm -rf -- "${app_bundle}"

if [[ "${remove_user_data}" -eq 1 ]]; then
  [[ "${data_root}" == "${HOME}/Library/Application Support/Pressay" ]] || exit 3
  rm -rf -- "${data_root}"
elif [[ "${remove_runtime}" -eq 1 ]]; then
  [[ "${runtime_root}" == "${HOME}/Library/Application Support/Pressay/venv" ]] || exit 3
  rm -rf -- "${runtime_root}"
fi

echo "Pressay application and autostart entry removed."
if [[ "${remove_user_data}" -eq 0 ]]; then
  echo "Configuration and model cache were preserved."
fi
