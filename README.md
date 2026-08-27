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
  <a href="docs/TROUBLESHOOTING.md">Troubleshooting</a> ·
  <a href="docs/TESTING.md">Testing status</a> ·
  <a href="CONTRIBUTING.md">Contributing</a>
</p>

## Status

| Platform | Status | Installation |
|---|---|---|
| Windows 11 | Stable, hardware-tested | `powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1` |
| macOS 13+ | Developer beta, CI-tested | `bash scripts/install-macos.sh` |

Pressay records from the selected microphone, recognizes Russian and English
locally with faster-whisper, and inserts the final text only if the same
editable control still owns focus. Audio and transcripts are not uploaded or
written to disk. On Windows, hotkeys (hold-to-talk, toggle, insert, copy) are
configurable in Pressay's settings window. The current macOS beta uses fixed
shortcuts; the guides below list them and the known keyboard-layout conflicts.

An opt-in voice command can switch subsequent RU/EN dictation to local English
translation. Translation uses a translation-capable model and may download that
model the first time it is selected; the normal language selector remains a
recognition hint and does not enable translation.

The macOS beta uses CPU inference. CTranslate2 supports Intel and Apple Silicon
macOS wheels, but its faster-whisper backend does not use Metal/MPS. A real Mac
is still required for the final microphone, Accessibility, Input Monitoring,
menu-bar and application-insertion acceptance checklist.

On Windows, installation publishes a versioned app payload under
`%LOCALAPPDATA%\Pressay\app\<version>` and points every managed shortcut at the
stable `%LOCALAPPDATA%\Pressay\Pressay.ps1` launcher. The installed app no
longer depends on the cloned repository. Each release uses a matching runtime
under `%LOCALAPPDATA%\Pressay\runtime\<version>`, while configuration, logs,
and the model cache remain shared. Upgrades retain the active release and one
previous app/runtime pair; older verified pairs are removed to avoid
accumulating multi-gigabyte runtimes. A stable `Uninstall-Pressay.ps1` is
installed beside the launcher.

The macOS developer beta still runs from its repository checkout; keep that
checkout in place.

## Quick start

### Windows

```powershell
git clone https://github.com/artemiyDev/Pressay.git
cd Pressay
.\scripts\install.ps1 -DesktopShortcut -EnableAutostart
```

Fully exit a running Pressay from its tray menu before installing or upgrading.

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
- Network access is expected when a selected recognition or translation model
  is not yet present in the local model cache.
- RU/EN only; automatic language selection never returns a third language.
- Choosing Russian or English skips language detection; it does not translate
  speech. Dictating in the other language may produce inaccurate text.
- No telemetry and no audio/transcript files.
- Focus fingerprint is rechecked before every insertion batch and Enter.
- Automatic insertion never falls back to overwriting the clipboard.
- On Windows, automatic insertion still uses the clipboard for multiline text and characters outside the Basic Multilingual Plane (emoji), restoring the previous content afterward; "Insert last" and "Copy" always use it. On macOS, automatic insertion and "Insert last" never touch the pasteboard — only "Copy" does.
- If Windows Clipboard History (Win+V) is on, a fragment delivered this way stays in the history after Pressay restores the clipboard, and syncs to other devices when cloud clipboard sync is on. Turn it off under **Settings → System → Clipboard** if that matters to you.
- Corrupt configuration disables automatic insertion instead of restoring unsafe defaults.

MIT licensed. See the full [English guide](docs/README.en.md),
[Russian guide](docs/README.ru.md), [troubleshooting guide](docs/TROUBLESHOOTING.md),
[security policy](SECURITY.md), and [changelog](CHANGELOG.md).
