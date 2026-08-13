# Testing status / Статус тестирования

## Verified now / Проверено сейчас

- Windows unit, lifecycle, installer and input-guard tests run locally.
- Windows application launches with the supplied Pressay icon.
- macOS modules are importable without loading native frameworks on Windows.
- macOS hotkey disambiguation and focus-change zero-injection rules have pure tests.
- GitHub Actions runs the full suite on `windows-2022` and Apple Silicon `macos-14`.
- macOS CI validates PyObjC framework imports and every `*-macos.sh` script with `bash -n`.

## Not proven without a real Mac / Без реального Mac не доказано

- the microphone permission prompt and actual PortAudio capture;
- Accessibility and Input Monitoring consent persistence for the source wrapper;
- global hotkeys while switching between native, Chromium and Electron apps;
- visible RU/EN insertion into TextEdit, Safari, Chrome, Telegram and VS Code;
- menu-bar icon behavior after login and wake from sleep;
- latency, thermals and model choice on each Apple Silicon generation;
- signed/notarized `.app` and `.dmg` distribution.

CI success means the macOS dependency graph, imports, pure state machines,
package assets and scripts are coherent. It does **not** replace the following
hardware acceptance run.

## Real-Mac acceptance checklist / Чек-лист на реальном Mac

1. Clone the public repository and run `bash scripts/install-macos.sh`.
2. Grant Microphone, Accessibility and Input Monitoring; restart Pressay.
3. Run `bash scripts/doctor-macos.sh --json` and save only the redacted result.
4. Select the intended microphone and run the in-app microphone test.
5. Dictate one Russian, one English and one mixed RU/EN phrase into TextEdit.
6. Repeat insertion in Safari/Chrome, Telegram and VS Code.
7. Confirm that changing focus before release causes zero insertion.
8. Confirm that an insertion failure does not alter rich clipboard content.
9. Enable autostart, log out/in, then verify one menu-bar instance and hotkeys.
10. Record warm recognition latency and memory for `small`; optionally compare `medium`.

Only after this checklist passes should macOS status change from developer beta
to hardware-tested.

## Source basis / Основание

- Apple requires [`NSMicrophoneUsageDescription`](https://developer.apple.com/documentation/BundleResources/Information-Property-List/NSMicrophoneUsageDescription) for microphone access.
- Apple Accessibility trust is checked through [`AXIsProcessTrustedWithOptions`](https://developer.apple.com/documentation/applicationservices/1459186-axisprocesstrustedwithoptions).
- Per-user background startup uses [`~/Library/LaunchAgents`](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html).
- [CTranslate2](https://opennmt.net/CTranslate2/installation.html) supports macOS x86-64 and ARM64 wheels; GPU wheels are for Linux/Windows.
