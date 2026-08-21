# Security policy / Политика безопасности

[English](#english) · [Русский](#русский)

## English

### Supported code

Security fixes are developed against the current `main` branch. The latest
tagged release can lag behind source, and older releases are not maintained.
This project does not promise a response or remediation deadline.

### Report a vulnerability privately

Do not publish exploit details, audio, transcripts, credentials, configuration
files, or unredacted logs in a public issue.

1. Open the repository's **Security** tab and use **Report a vulnerability** if
   private vulnerability reporting is available.
2. Include the affected version or commit, platform, impact, minimal
   reproduction, and a suggested mitigation when known.
3. If no private reporting option is available, open a public issue containing
   only a request for a private security contact. Do not include vulnerability
   details in that issue.

Relevant reports include unsafe text insertion, clipboard disclosure, leakage
of audio or transcripts, unintended network access, permission bypasses,
installer or update tampering, and dependency vulnerabilities with a practical
Pressay impact.

Pressay has no bug bounty program. Please allow maintainers time to reproduce
and prepare a fix before public disclosure.

### Privacy expectations

Recognition and translation run locally once the needed model files are in the
local cache. Downloading a selected uncached recognition or translation model
is expected network access. Pressay is designed not to upload audio or
transcripts and has no telemetry. Logs should not contain audio, transcript
text, or window titles, but they can contain device details, timings, errors,
and local paths; review and redact them before sharing.

## Русский

### Поддерживаемый код

Исправления безопасности готовятся для текущей ветки `main`. Последний тег
может отставать от исходного кода, старые версии не поддерживаются. Проект не
обещает конкретного срока ответа или исправления.

### Как сообщить об уязвимости приватно

Не публикуйте в открытом issue детали эксплуатации, аудио, расшифровки,
учётные данные, конфиги или неочищенные логи.

1. Откройте вкладку **Security** репозитория и выберите
   **Report a vulnerability**, если приватные отчёты доступны.
2. Укажите версию или commit, платформу, влияние, минимальное воспроизведение и,
   если известно, способ снизить риск.
3. Если приватного канала нет, создайте публичный issue только с просьбой дать
   приватный контакт. Не раскрывайте в нём детали уязвимости.

В область интереса входят небезопасная вставка текста, раскрытие буфера обмена,
утечка аудио или расшифровок, неожиданная сеть, обход разрешений, подмена
установки или обновления и уязвимости зависимостей с практическим влиянием на
Pressay.

В проекте нет bug bounty. Дайте сопровождающим время воспроизвести проблему и
подготовить исправление до публичного раскрытия.

### Ожидания приватности

Распознавание и перевод работают локально, когда нужные файлы моделей уже есть
в локальном кэше. Скачивание выбранной модели распознавания или перевода,
которой ещё нет в кэше, — ожидаемая сетевая операция. Pressay не должен
отправлять аудио или расшифровки и не использует телеметрию. Логи не должны
содержать аудио, текст расшифровок или заголовки окон, но могут содержать
сведения об устройствах, тайминги, ошибки и локальные пути — проверьте и
очистите их перед отправкой.
