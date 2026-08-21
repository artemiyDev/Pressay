# Changelog / История изменений

Notable user-visible changes are recorded here. Dates use `YYYY-MM-DD`.

Здесь перечислены заметные пользовательские изменения. Даты указаны в формате
`YYYY-MM-DD`.

`0.5.0` is the version declared by the current source tree; it does not yet
have a matching tagged release. Версия `0.5.0` указана в текущем исходном коде,
но соответствующего тега выпуска пока нет.

## Unreleased / Не выпущено

### Added / Добавлено

- Added an opt-in voice-controlled session mode that translates subsequent
  RU/EN dictation to English with a local translation-capable model.
- Добавлен необязательный голосовой режим сессии, переводящий следующую
  RU/EN-диктовку на английский через локальную модель с поддержкой перевода.

### Changed / Изменено

- Windows installation now publishes versioned application payloads under
  `%LOCALAPPDATA%\Pressay\app`, activates them through a `current` pointer, and
  uses the stable `%LOCALAPPDATA%\Pressay\Pressay.ps1` launcher for start-menu,
  desktop, and autostart shortcuts. Installing or upgrading requires Pressay to
  be fully exited.
- Windows releases now pair each payload with a versioned runtime and activate
  both through the same atomic pointer. Failed dependency installation cannot
  mutate the active release, and a source-independent uninstaller is installed.
- Windows uninstall now removes only Pressay-owned shortcuts by default. The
  explicit `-RemoveApp`, `-RemoveRuntime`, `-RemoveUserData`, and
  `-RemoveInstaller` flags remove installed payloads, versioned/legacy runtimes,
  configuration/logs, and the installed uninstaller respectively; model caches
  remain preserved.
- The settings window now adapts to narrow and small screens, keeps its main
  actions visible, scrolls focused controls into view, and exposes complete
  keyboard and accessibility navigation.
- Установка Windows теперь публикует версионные копии приложения в
  `%LOCALAPPDATA%\Pressay\app`, переключает указатель `current` и использует
  стабильный `%LOCALAPPDATA%\Pressay\Pressay.ps1` для ярлыков меню «Пуск»,
  рабочего стола и автозапуска. Перед установкой или обновлением Pressay нужно
  полностью завершить.
- Каждый Windows payload теперь связан со своим версионным runtime и
  активируется тем же атомарным указателем. Неудачная установка зависимостей не
  изменяет активную версию; uninstaller устанавливается независимо от исходников.
- По умолчанию удаление Windows теперь убирает только принадлежащие Pressay
  ярлыки. Флаги `-RemoveApp`, `-RemoveRuntime`, `-RemoveUserData` и
  `-RemoveInstaller` отдельно удаляют копии приложения, версионные/legacy
  runtime, конфигурацию/логи и установленный uninstaller; кэш моделей
  сохраняется.
- Окно настроек теперь адаптируется к узким и небольшим экранам, оставляет
  основные действия видимыми, прокручивает к элементу с фокусом и поддерживает
  полную клавиатурную навигацию и accessibility.

### Fixed / Исправлено

- On macOS, `Esc` now passes through when Pressay has no cancellable dictation;
  its key-down and key-up are suppressed only for an accepted cancellation.
- На macOS `Esc` теперь проходит в активное приложение, если Pressay нечего
  отменять; обе фазы клавиши подавляются только при принятой отмене диктовки.

### Documentation / Документация

- Corrected RU/EN descriptions of transcript history, language selection,
  hotkey conflicts, macOS shortcuts, Windows/macOS installation topology, and
  CI scope.
- Added troubleshooting, contribution, security, issue, and pull request
  guidance.
- Исправлены RU/EN описания истории, выбора языка, конфликтов клавиш,
  сочетаний macOS, схемы установки Windows/macOS и границ проверки CI.
- Добавлены руководства по диагностике, участию, безопасности, issues и pull
  requests.

## 0.3.0 - 2026-08-20

### Added / Добавлено

- Configurable Windows push-to-talk, hands-free, insert-last, and copy hotkeys.
- Dark settings window, live microphone level, model activity and first-download
  progress.
- Memory-only history for up to 20 transcripts with explicit copy.
- Optional voice formatting for Enter, new line, and paragraph.
- Настраиваемые горячие клавиши Windows, тёмное окно настроек, индикатор уровня
  микрофона, состояния модели и прогресс первого скачивания.
- История до 20 расшифровок только в памяти и необязательные голосовые команды
  форматирования.

### Changed / Изменено

- Model loading stays offline when weights already exist locally.
- Short push-to-talk recordings skip VAD filtering.
- Microphone pre-arm and asynchronous stream closing reduce clipped first words
  and release-to-recognition delay.
- Загрузка модели остаётся офлайн при наличии весов; короткая диктовка не
  фильтруется VAD; предварительное открытие микрофона и фоновое закрытие потока
  сокращают потерю первых слов и задержку после отпускания клавиш.

### Fixed / Исправлено

- Editable controls wrapped in Windows `Pane` elements can be accepted using
  additional editability and caret evidence.
- Focus is rechecked before insertion so text is not sent to a different
  control after recognition.
- Улучшено определение редактируемых Windows-полей внутри `Pane`; перед
  вставкой повторно проверяется фокус.

## 0.2.0 - 2026-08-14

- Initial public Windows application and macOS developer beta.
- Local faster-whisper recognition, tray/menu-bar operation, installers,
  focus-guarded insertion, bilingual guides, and cross-platform CI.
- Первая публичная Windows-версия и developer beta для macOS: локальное
  распознавание, трей/menu bar, установщики, безопасная проверка фокуса,
  двуязычные руководства и кроссплатформенный CI.
