<p align="center">
  <img src="src/pressay/assets/wordmark.svg" alt="Pressay" width="760">
</p>

<p align="center"><strong>Press → say.</strong> Private, local-first voice dictation for Windows and macOS.</p>

<p align="center">
  <a href="https://github.com/artemiyDev/Pressay/actions/workflows/ci.yml"><img src="https://github.com/artemiyDev/Pressay/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
</p>

<p align="center">
  <a href="docs/README.en.md">English</a> ·
  <a href="docs/README.ru.md">Русский</a> ·
  <a href="docs/TESTING.md">Testing status</a>
</p>

## Status

| Platform | Status | Installation |
|---|---|---|
| Windows 11 | Stable, hardware-tested | `powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1` |
| macOS 13+ | Developer beta, CI-tested | `bash scripts/install-macos.sh` |

Pressay records from the selected microphone, recognizes Russian and English
locally with faster-whisper, and inserts the final text only if the same
editable control still owns focus. Audio and transcripts are not uploaded or
written to disk.

The macOS beta uses CPU inference. CTranslate2 supports Intel and Apple Silicon
macOS wheels, but its faster-whisper backend does not use Metal/MPS. A real Mac
is still required for the final microphone, Accessibility, Input Monitoring,
menu-bar and application-insertion acceptance checklist.

## Quick start

### Windows

```powershell
git clone https://github.com/artemiyDev/Pressay.git
cd Pressay
.\scripts\install.ps1 -DesktopShortcut -EnableAutostart
```

### macOS

```bash
git clone https://github.com/artemiyDev/Pressay.git
cd Pressay
bash scripts/install-macos.sh
```

On macOS, grant Pressay access under **System Settings → Privacy & Security**:
Microphone, Accessibility, and Input Monitoring. Restart Pressay after changing
Accessibility or Input Monitoring permissions.

## Privacy and safety

- Local ASR after the model has been downloaded.
- RU/EN only; automatic language selection never returns a third language.
- No telemetry and no audio/transcript files.
- Focus fingerprint is rechecked before every insertion batch and Enter.
- Automatic insertion never falls back to overwriting the clipboard.
- Corrupt configuration disables automatic insertion instead of restoring unsafe defaults.

MIT licensed. See the full [English guide](docs/README.en.md) or
[Russian guide](docs/README.ru.md).
