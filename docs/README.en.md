# Pressay — English guide

[Русский](README.ru.md) · [Testing status](TESTING.md) · [Home](../README.md)

**Press → say.** Pressay is local voice dictation for Windows and macOS. After
the initial model download, recognition runs without a cloud API. Russian and
English are the only supported languages.

## Features

- push-to-talk and hands-free global hotkeys;
- local faster-whisper recognition;
- personal vocabulary and deterministic term replacements;
- focused editable-control verification before every insertion batch;
- only two recent transcripts retained in application memory;
- explicit copy, with no hidden clipboard overwrite after insertion failures.

## Windows 11 — stable

Requirements: Windows 11 x64, Python 3.11, and a working microphone. NVIDIA GPU
acceleration is optional; Pressay falls back to CPU.

```powershell
git clone https://github.com/artemiyDev/Pressay.git
cd Pressay
.\scripts\install.ps1 -DesktopShortcut -EnableAutostart
```

The non-admin installer creates `%LOCALAPPDATA%\Pressay\venv`, installs the
dependencies, downloads the `turbo` model, creates managed shortcuts, and
starts Pressay in the system tray.

```powershell
.\scripts\install.ps1 -NoLaunch
.\scripts\install.ps1 -Model small
.\scripts\doctor.ps1
.\scripts\test.ps1 -q
```

| Action | Windows hotkey |
|---|---|
| Hold to dictate | `Ctrl+Win` |
| Toggle hands-free | `Ctrl+Win+Space` |
| Cancel | `Esc` |
| Insert the last transcript | `Shift+Alt+Z` |
| Copy the last transcript | `Shift+Alt+X` |

## macOS 13+ — developer beta

Requirements: Intel or Apple Silicon Mac, macOS 13+, and Python 3.11. The beta
runs in GitHub Actions on an Apple Silicon macOS runner, but has not yet passed
the real-hardware acceptance checklist on a user Mac.

```bash
git clone https://github.com/artemiyDev/Pressay.git
cd Pressay
bash scripts/install-macos.sh
```

The installer creates `~/Library/Application Support/Pressay/venv`, installs
PySide6, faster-whisper, sounddevice and PyObjC, warms the CPU `small` model,
and creates `~/Applications/Pressay.app`. Autostart is opt-in:

```bash
bash scripts/install-macos.sh --enable-autostart
bash scripts/install-macos.sh --model medium
bash scripts/doctor-macos.sh
bash scripts/test-macos.sh
```

CTranslate2 publishes Intel and ARM64 macOS wheels, but faster-whisper does not
use Metal/MPS. The beta therefore uses CPU `int8` inference.

### macOS permissions

Under **System Settings → Privacy & Security**, allow Pressay access to:

1. **Microphone** for speech capture;
2. **Accessibility** for editable-control verification and Unicode input;
3. **Input Monitoring** for global hotkeys.

Restart Pressay after changing Accessibility or Input Monitoring. In the
source beta, macOS may identify the runtime Python executable instead of the
wrapper app. A signed and notarized `.dmg` is a separate release milestone.

| Action | macOS hotkey |
|---|---|
| Hold to dictate | `Control+Option` |
| Toggle hands-free | `Control+Option+Space` |
| Cancel | `Esc` |
| Insert the last transcript | `Control+Option+V` |
| Copy the last transcript | `Control+Option+C` |

## Uninstall

```powershell
.\scripts\uninstall.ps1
```

```bash
bash scripts/uninstall-macos.sh
```

Configuration and model caches are preserved unless a destructive removal flag
is explicitly supplied. See the scripts' `--help` output for details.

## Data locations

| Data | Windows | macOS |
|---|---|---|
| Configuration | `%LOCALAPPDATA%\Pressay\config.json` | `~/Library/Application Support/Pressay/config.json` |
| Runtime | `%LOCALAPPDATA%\Pressay\venv` | `~/Library/Application Support/Pressay/venv` |
| Logs | `%LOCALAPPDATA%\Pressay\pressay.log` | `~/Library/Application Support/Pressay/pressay.log` |

Logs never contain transcript text, audio, or window titles.
