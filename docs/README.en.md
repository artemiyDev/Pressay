# Pressay — English guide

[Русский](README.ru.md) · [Troubleshooting](TROUBLESHOOTING.md) ·
[Testing status](TESTING.md) · [Home](../README.md)

**Press → say.** Pressay is local voice dictation for Windows and macOS. Once
the selected model is present in the local cache, recognition runs without a
cloud API. Russian and English are the only supported languages.

## Features

- configurable Windows push-to-talk and hands-free global hotkeys, plus fixed
  shortcuts in the macOS beta, with conflict guidance for keyboard layouts;
- local faster-whisper recognition;
- personal vocabulary and deterministic term replacements;
- optional voice commands when enabled: “press Enter”, Russian “с новой строки” (“new line”) and “абзац” (“paragraph”);
- optional voice-controlled translation of subsequent RU/EN dictation to
  English, performed locally with a translation-capable model;
- focused editable-control verification before every insertion batch;
- up to 20 recent transcripts retained only in application memory until exit;
- explicit copy, with no hidden clipboard overwrite after insertion failures;
- on Windows, automatic insertion still uses the clipboard for multiline text and characters outside the Basic Multilingual Plane (emoji), restoring the previous content afterward — "Insert last" and "Copy" always use it; on macOS, automatic insertion and "Insert last" never touch the pasteboard, only "Copy" does;
- if Windows Clipboard History (Win+V) is on, a fragment delivered this way stays in the history after Pressay restores the clipboard, and syncs to other devices when cloud clipboard sync is on — turn it off under **Settings → System → Clipboard** if that matters to you.

Selecting “Русский” or “English” skips automatic language detection and can
speed up recognition. It is an ASR hint, not translation: dictating in the
other language may produce inaccurate text.

Translation is a separate opt-in session mode. Enable **Голосовое переключение
перевода на английский** in settings, then say the whole phrase “translate to
English” or “переведи на английский” to turn it on. Say “stop translating” or
“хватит переводить” to turn it off. The recording overlay shows **→ EN** while
the mode is active. `turbo` cannot translate, so Pressay uses the selected
`small`, `medium`, or `large-v3` translation model; an uncached model is
downloaded when first needed.

## Windows 11 — stable

Requirements: Windows 11 x64, Python 3.11, and a working microphone. NVIDIA GPU
acceleration is optional; Pressay falls back to CPU.

```powershell
git clone https://github.com/artemiyDev/Pressay.git
cd Pressay
.\scripts\install.ps1 -DesktopShortcut -EnableAutostart
```

Fully exit a running Pressay from its tray menu before installing or upgrading;
closing only the settings window is not enough. The installer refuses to change
the app while it is running.

The non-admin installer publishes a versioned application payload under
`%LOCALAPPDATA%\Pressay\app\<version>`, activates it through the small `current`
pointer, and installs the stable `%LOCALAPPDATA%\Pressay\Pressay.ps1` launcher.
It also creates `%LOCALAPPDATA%\Pressay\venv`, installs the dependencies,
downloads the `turbo` model, creates managed shortcuts, and starts Pressay in
the system tray. Start-menu, desktop, and autostart shortcuts point to the
stable launcher, so a successful Windows installation keeps working after the
cloned repository is moved or deleted. Run later upgrades or maintenance
scripts from a fresh Pressay source tree when needed. Upgrades reuse the shared
runtime and preserve configuration, logs, and the shared model cache outside
the versioned payload.

```powershell
.\scripts\install.ps1 -NoLaunch
.\scripts\install.ps1 -Model small
.\scripts\doctor.ps1
.\scripts\test.ps1 -q
```

Windows hotkeys are configurable in Pressay's settings window, in the
"Горячие клавиши" ("Hotkeys") group; the table below lists the shipped
defaults.

| Action | Windows hotkey (default) |
|---|---|
| Hold to dictate | `Ctrl+Win` |
| Toggle hands-free | `Ctrl+Win+Space` |
| Cancel | `Esc` |
| Insert the last transcript | `Shift+Alt+Z` |
| Copy the last transcript | `Shift+Alt+X` |

The hold-to-talk combination is chosen from six modifier pairs (`ctrl`,
`win`, `shift`, `alt`). The toggle key, "Insert last" and "Copy" are entered
as text of the form `modifier+modifier+key` (for example, `shift+alt+z`):
parts are joined with `+`, modifiers are `ctrl`, `win`, `shift`, `alt`, and
the regular key is a letter, a digit, `space`, or `f1`–`f12`. The word
`none` disables an action. Push-to-talk can be turned off — then recording
starts and stops only with the toggle-key combination, without holding
anything down. Cancel (`Esc`) is not configurable.

Conflict warning: `Ctrl+Alt` is AltGr on many keyboard layouts and is used to
type characters; `Ctrl+Shift` and `Shift+Alt` often switch the Windows layout.
`Ctrl+Win` uses the Windows system key and can also overlap a Windows or
application shortcut. There is no universally conflict-free pair; choose one
that is free in your applications and keyboard layouts.

## macOS 13+ — developer beta

Requirements: Intel or Apple Silicon Mac, macOS 13+, and Python 3.11. The beta
runs in GitHub Actions on an Apple Silicon macOS runner, but has not yet passed
the real-hardware acceptance checklist on a user Mac.

```bash
git clone https://github.com/artemiyDev/Pressay.git
cd Pressay
bash scripts/install-macos.sh
```

The app wrapper also launches from this repository checkout. Keep the checkout
in place until you reinstall Pressay from a new location.

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

The current macOS beta uses fixed shortcuts. The hotkey fields in Pressay's
settings are Windows-only and do not change these macOS shortcuts.

| Action | Fixed macOS hotkey |
|---|---|
| Hold to dictate | `Control+Option` |
| Toggle hands-free | `Control+Option+Space` |
| Cancel | `Esc` |
| Insert the last transcript | `Control+Option+V` |
| Copy the last transcript | `Control+Option+C` |

## Uninstall

On Windows, the default command removes only Pressay-owned start-menu, desktop,
and autostart shortcuts. It preserves the installed application, runtime,
configuration, logs, and models. Fully exit Pressay from its tray menu before
using any of the removal flags below.

```powershell
.\scripts\uninstall.ps1
.\scripts\uninstall.ps1 -RemoveApp
.\scripts\uninstall.ps1 -RemoveRuntime
.\scripts\uninstall.ps1 -RemoveUserData
.\scripts\uninstall.ps1 -RemoveApp -RemoveRuntime -RemoveUserData
```

`-RemoveApp` removes the installed versioned payloads, `current` pointer,
launcher, and icon. `-RemoveRuntime` removes the shared Pressay virtual
environment. `-RemoveUserData` permanently removes configuration and logs.
These flags can be combined. The shared Hugging Face model cache is never
removed by this script.

```bash
bash scripts/uninstall-macos.sh
```

The macOS uninstall script also preserves configuration unless its user-data
flag is supplied, and it never removes the shared Hugging Face model cache.
See the scripts' help output for details.

## Data locations

| Data | Windows | macOS |
|---|---|---|
| Installed application | `%LOCALAPPDATA%\Pressay\app\<version>` | Repository checkout (developer beta) |
| Active-version pointer | `%LOCALAPPDATA%\Pressay\current` | — |
| Stable launcher | `%LOCALAPPDATA%\Pressay\Pressay.ps1` | `~/Applications/Pressay.app` (repository-backed) |
| Configuration | `%LOCALAPPDATA%\Pressay\config.json` | `~/Library/Application Support/Pressay/config.json` |
| Runtime | `%LOCALAPPDATA%\Pressay\venv` | `~/Library/Application Support/Pressay/venv` |
| Logs | `%LOCALAPPDATA%\Pressay\pressay.log` | `~/Library/Application Support/Pressay/pressay.log` |
| Models | Shared Hugging Face cache | Shared Hugging Face cache |

Logs never contain transcript text, audio, or window titles.

If something is not working, start with the bilingual
[troubleshooting guide](TROUBLESHOOTING.md).
