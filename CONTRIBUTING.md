# Contributing to Pressay / Участие в разработке Pressay

[English](#english) · [Русский](#русский)

Pressay welcomes focused bug fixes, tests, documentation, and platform
improvements. Privacy and safe text insertion are product requirements, not
optional conventions.

## English

### Before you start

1. Search existing issues and pull requests to avoid duplicate work.
2. For a substantial behavior or interface change, open an issue first and
   describe the user problem and acceptance criteria.
3. Keep each pull request limited to one independently reviewable change.

Do not include audio, transcripts, credentials, full configuration files,
unredacted logs, or machine-specific paths in an issue, test fixture, commit,
or pull request.

### Development setup

Pressay requires Python 3.11. On Windows:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python.exe -m pytest -q
```

On macOS:

```bash
python3.11 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -e '.[dev,macos]'
QT_QPA_PLATFORM=offscreen ./.venv/bin/python -m pytest -q
```

A real transcription with a model that is absent from the local cache needs a
model download. Automated tests replace the model and do not require a
microphone, CUDA, or a model download.

### Pull request checklist

- Add or update tests for behavior changes.
- Run the full pytest command with zero failures.
- Run `git diff --check`.
- Update both RU and EN user documentation when behavior changes.
- Explain what was tested on real Windows or macOS hardware. CI is not a
  substitute for microphone, global-hotkey, permission, or insertion checks.
- Preserve local-first operation: no telemetry, transcript upload, or audio
  upload. Expected network access is limited to downloading a selected
  recognition or translation model that is absent from the local cache.
- Preserve fail-closed insertion: uncertain focus must never cause typing into
  an unrelated control or silent clipboard fallback.

Use the [bug report template](.github/ISSUE_TEMPLATE/bug_report.yml) for
reproducible problems. Follow [SECURITY.md](SECURITY.md) for vulnerabilities.

## Русский

### Перед началом

1. Проверьте существующие issues и pull requests, чтобы не дублировать работу.
2. Перед существенным изменением поведения или интерфейса создайте issue с
   описанием пользовательской проблемы и проверяемыми критериями готовности.
3. Ограничивайте каждый pull request одним независимо проверяемым изменением.

Не добавляйте в issue, тесты, коммиты или pull request аудио, расшифровки,
учётные данные, полные конфиги, неочищенные логи и локальные пути компьютера.

### Среда разработки

Нужен Python 3.11. Команды установки и тестирования приведены выше для Windows
и macOS. Реальная расшифровка требует загрузки выбранной модели, если её нет в
локальном кэше, но автоматические тесты подменяют модель и не требуют
микрофона, CUDA или скачивания весов.

### Что проверить перед pull request

- Добавьте или обновите тесты для изменённого поведения.
- Запустите полный pytest без падений и `git diff --check`.
- При изменении поведения обновите русскую и английскую документацию.
- Укажите, что проверено на реальном Windows или Mac: CI не заменяет проверку
  микрофона, глобальных сочетаний, разрешений и вставки в приложения.
- Сохраните local-first модель: без телеметрии и отправки аудио или текста.
  Ожидаемая сеть ограничена скачиванием выбранной модели распознавания или
  перевода, которой ещё нет в локальном кэше.
- Сохраните безопасную вставку: при сомнении в фокусе Pressay не должен печатать
  в другое поле или незаметно переходить на буфер обмена.

Для воспроизводимых ошибок используйте
[шаблон bug report](.github/ISSUE_TEMPLATE/bug_report.yml), а для уязвимостей —
инструкции из [SECURITY.md](SECURITY.md).
