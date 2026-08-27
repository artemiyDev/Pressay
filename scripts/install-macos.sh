#!/usr/bin/env bash
set -euo pipefail

model="small"
skip_model=0
enable_autostart=0
no_launch=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model) model="${2:?--model requires a value}"; shift 2 ;;
    --skip-model) skip_model=1; shift ;;
    --enable-autostart) enable_autostart=1; shift ;;
    --no-launch) no_launch=1; shift ;;
    -h|--help)
      echo "Usage: bash scripts/install-macos.sh [--model small] [--skip-model] [--enable-autostart] [--no-launch]"
      exit 0
      ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Pressay macOS installer can only run on macOS." >&2
  exit 2
fi

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
setup_args=(--model "${model}")
if [[ "${skip_model}" -eq 1 ]]; then setup_args+=(--skip-model); fi
bash "${project_root}/scripts/setup-macos.sh" "${setup_args[@]}"

applications_root="${HOME}/Applications"
app_bundle="${applications_root}/Pressay.app"
contents="${app_bundle}/Contents"
launcher="${contents}/MacOS/Pressay"
mkdir -p "${contents}/MacOS" "${contents}/Resources"

icon_source="${project_root}/src/pressay/assets/app-icon.svg"
iconset="${HOME}/Library/Application Support/Pressay/Pressay.iconset"
rm -rf -- "${iconset}"
mkdir -p "${iconset}"
PRESSAY_ICON_SOURCE="${icon_source}" PRESSAY_ICONSET="${iconset}" \
  "${HOME}/Library/Application Support/Pressay/venv/bin/python" - <<'PY'
import os
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

source = QImage(os.environ["PRESSAY_ICON_SOURCE"])
target = Path(os.environ["PRESSAY_ICONSET"])
for size in (16, 32, 128, 256, 512):
    for scale in (1, 2):
        pixels = size * scale
        name = f"icon_{size}x{size}" + ("@2x" if scale == 2 else "") + ".png"
        image = source.scaled(pixels, pixels, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        if image.isNull() or not image.save(str(target / name), "PNG"):
            raise SystemExit(f"Could not render {name}")
PY
iconutil -c icns "${iconset}" -o "${contents}/Resources/Pressay.icns"
rm -rf -- "${iconset}"

cat > "${contents}/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDisplayName</key><string>Pressay</string>
  <key>CFBundleExecutable</key><string>Pressay</string>
  <key>CFBundleIdentifier</key><string>dev.pressay.app</string>
  <key>CFBundleIconFile</key><string>Pressay</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>CFBundleName</key><string>Pressay</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.5.3</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key>
  <string>Pressay uses the microphone only for local speech recognition. Audio is not uploaded or saved.</string>
</dict>
</plist>
PLIST

printf '#!/usr/bin/env bash\nexec %q --background\n' "${project_root}/scripts/run-macos.sh" > "${launcher}"
chmod 755 "${launcher}" "${project_root}/scripts/"*.sh

if [[ "${enable_autostart}" -eq 1 ]]; then
  agent_dir="${HOME}/Library/LaunchAgents"
  agent_path="${agent_dir}/dev.pressay.app.plist"
  mkdir -p "${agent_dir}"
  cat > "${agent_path}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>dev.pressay.app</string>
  <key>ProgramArguments</key>
  <array><string>${launcher}</string></array>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Interactive</string>
</dict>
</plist>
PLIST
  launchctl bootout "gui/${UID}" "${agent_path}" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/${UID}" "${agent_path}"
  echo "Pressay autostart enabled for the current user."
fi

if [[ "${no_launch}" -eq 0 ]]; then
  open "${app_bundle}"
fi

echo "Pressay installed at ${app_bundle}"
echo "Grant Microphone, Accessibility, and Input Monitoring permissions when macOS asks."
