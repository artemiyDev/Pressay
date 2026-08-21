# Troubleshooting / Решение проблем

[English](#english) · [Русский](#русский) · [Home / Главная](../README.md)

## English

### Start with a local diagnostic

Run the command from the repository directory:

```powershell
.\scripts\doctor.ps1
```

```bash
bash scripts/doctor-macos.sh
```

The log is at `%LOCALAPPDATA%\Pressay\pressay.log` on Windows and
`~/Library/Application Support/Pressay/pressay.log` on macOS. Logs are designed
not to contain transcript text, audio, or window titles, but may contain local
paths and device details. Review and redact them before sharing.

On Windows, installed shortcuts launch
`%LOCALAPPDATA%\Pressay\Pressay.ps1`, which follows the `current` pointer to a
versioned payload under `%LOCALAPPDATA%\Pressay\app`. The installed Windows app
does not depend on the cloned repository. The macOS developer beta still runs
from its checkout; if that directory was moved or deleted, install the beta
again from its new permanent location.

Before running the Windows setup or install script, fully exit Pressay from its
tray menu. Closing the settings window leaves the tray application running, and
the installer will refuse to continue.

### “Speech not detected”

1. Open Pressay settings and select the microphone you are actually speaking
   into. “System default” follows the operating-system default input.
2. Use **Проверить микрофон**. This confirms that Pressay can open the selected
   device; it is not a full speech-recognition or loudness test.
3. On Windows, allow microphone access for desktop apps under
   **Settings → Privacy & security → Microphone**.
4. On macOS, allow Microphone access under
   **System Settings → Privacy & Security**, then restart Pressay.
5. Speak before releasing push-to-talk and keep the microphone close enough to
   produce a normal input level in the overlay.

If the device opens but the level never moves, verify the selected input and
its mute/gain controls in the operating system or device software.

### The text was recognized but not inserted

Pressay records the focused editable control when dictation starts and checks
it again before insertion. Switching windows or fields can therefore refuse
the insertion intentionally.

- Click the intended text field, start a new dictation, and keep that field
  focused until insertion finishes.
- Open settings and use the memory-only history (up to 20 entries) to copy the
  result explicitly or use the “Insert last” shortcut.
- If Pressay repeatedly rejects a real text field, include the application name
  and a minimal reproduction in a bug report. Do not include dictated text.

Automatic insertion does not silently copy text after a failure. On Windows,
multiline and non-BMP text can use a short clipboard transaction; see the
privacy notes in the [English guide](README.en.md#features).

### The Windows key appears held or a shortcut conflicts

No global modifier pair is conflict-free in every layout and application.
`Ctrl+Alt` can act as AltGr, `Ctrl+Shift` and `Shift+Alt` often switch layouts,
and `Ctrl+Win` can overlap Windows or application shortcuts.

- Press and release the affected physical modifier once.
- Choose a different pair in **Горячие клавиши** and test it in the applications
  where you dictate.
- Disable push-to-talk and use the hands-free toggle if every hold pair conflicts
  with your workflow.

### Recognition is slow or uses too many GPU resources

- **Instant** keeps the model resident and gives the fastest next dictation at
  the highest steady resource use.
- **Balanced** and **Eco** release resources more aggressively and make a later
  dictation wait for model loading.
- On Windows, Pressay automatically tries CUDA and falls back to CPU if CUDA
  cannot load or inference fails. There is currently no manual CPU-only switch
  in settings. CPU inference is normally slower; numerical backends and timing
  can produce small output differences with the same model.
- A smaller model is faster and lighter but can reduce recognition quality.
- Selecting a fixed RU or EN input language skips detection. It does not
  translate speech in the other language and can make that speech inaccurate.

The first use of a missing model requires internet access. Later recognition
uses local cached model files.

### Translation does not turn on or its first use is slow

- Enable **Голосовое переключение перевода на английский** in settings.
- Say “translate to English” or “переведи на английский” as a complete phrase.
  The recording overlay shows **→ EN** after the mode has been enabled.
- `turbo` cannot translate. Pressay uses the selected `small`, `medium`, or
  `large-v3` translation model, which needs a one-time download if it is not
  already cached.
- Say “stop translating” or “хватит переводить” as a complete phrase to return
  to ordinary recognition.

### macOS beta limitations

- Grant Microphone, Accessibility, and Input Monitoring, then fully restart
  Pressay after changing Accessibility or Input Monitoring.
- macOS shortcuts are currently fixed at `Control+Option`,
  `Control+Option+Space`, `Control+Option+V`, and `Control+Option+C`. The hotkey
  fields in settings are Windows-only.
- Recognition uses CPU; faster-whisper/CTranslate2 does not use Metal/MPS.
- CI checks imports, state machines, scripts, and tests. Real microphone,
  permissions, global shortcuts, and insertion still require a real Mac; see
  the [hardware checklist](TESTING.md#real-mac-acceptance-checklist--чек-лист-на-реальном-mac).

## Русский

### Начните с локальной диагностики

Из папки репозитория запустите:

```powershell
.\scripts\doctor.ps1
```

```bash
bash scripts/doctor-macos.sh
```

Лог находится в `%LOCALAPPDATA%\Pressay\pressay.log` на Windows и в
`~/Library/Application Support/Pressay/pressay.log` на macOS. В нём не должно
быть аудио, текста расшифровок и заголовков окон, но могут быть локальные пути
и сведения об устройствах. Проверьте и очистите лог перед отправкой.

На Windows установленные ярлыки запускают стабильный
`%LOCALAPPDATA%\Pressay\Pressay.ps1`. Он читает указатель `current` и открывает
версионную копию приложения из `%LOCALAPPDATA%\Pressay\app`; клонированный
репозиторий для запуска больше не нужен. Developer beta для macOS по-прежнему
работает из копии репозитория: если её переместили или удалили, установите beta
заново из нового постоянного расположения.

Перед запуском Windows-скрипта setup или install полностью завершите Pressay
через меню в трее. Закрытие окна настроек не завершает приложение, поэтому
установщик откажется продолжать работу.

### «Речь не обнаружена»

1. В настройках Pressay выберите микрофон, в который говорите. «Системный
   микрофон по умолчанию» следует системному устройству ввода.
2. Нажмите **Проверить микрофон**. Проверка подтверждает, что выбранное
   устройство открывается; это не полный тест громкости или распознавания.
3. На Windows разрешите микрофон для классических приложений в
   **Параметры → Конфиденциальность и безопасность → Микрофон**.
4. На macOS разрешите Microphone в **System Settings → Privacy & Security** и
   перезапустите Pressay.
5. Начните говорить до отпускания push-to-talk и проверьте, что индикатор уровня
   в оверлее заметно двигается.

Если устройство открывается, но уровень не меняется, проверьте выбранный вход,
mute и gain в системе или программе самого устройства.

### Текст распознан, но не вставлен

Pressay запоминает активное редактируемое поле в начале диктовки и проверяет его
перед вставкой. Если переключить окно или поле, приложение намеренно откажется
вставлять текст.

- Кликните по нужному полю, начните новую диктовку и не меняйте фокус до конца
  вставки.
- В настройках откройте историю только в памяти (до 20 записей), скопируйте
  результат явно или используйте сочетание «Вставить последнюю».
- Если настоящее поле постоянно отклоняется, укажите приложение и минимальные
  шаги в bug report. Не прикладывайте продиктованный текст.

После ошибки автовставка не копирует текст в буфер скрытно. На Windows
многострочный текст и символы вне BMP могут использовать короткую транзакцию
буфера; детали есть в [русском руководстве](README.ru.md#возможности).

### Клавиша Windows выглядит зажатой или сочетание конфликтует

Нет пары модификаторов без конфликтов во всех раскладках и приложениях.
`Ctrl+Alt` может работать как AltGr, `Ctrl+Shift` и `Shift+Alt` часто меняют
раскладку, а `Ctrl+Win` может пересекаться с сочетаниями Windows или приложения.

- Один раз нажмите и отпустите физическую клавишу-модификатор.
- Выберите другую пару в разделе **Горячие клавиши** и проверьте её во всех
  нужных приложениях.
- Если конфликтуют все пары удержания, выключите push-to-talk и используйте
  переключатель hands-free.

### Распознавание медленное или занимает много GPU

- «Мгновенно» держит модель в памяти, поэтому следующая диктовка начинается
  быстрее, но постоянно занимает больше ресурсов.
- «Сбалансированно» и «Экономно» активнее освобождают ресурсы, а следующая
  диктовка может ждать загрузки модели.
- На Windows Pressay автоматически сначала пробует CUDA, а при ошибке загрузки
  или распознавания переключается на CPU. Ручного переключателя «только CPU» в
  настройках пока нет. CPU обычно медленнее; вычислительный backend и тайминги
  могут дать небольшие различия результата на той же модели.
- Меньшая модель быстрее и легче, но может снизить качество распознавания.
- Фиксированный RU или EN пропускает определение языка. Это не перевод: речь на
  другом языке может распознаться неточно.

Первый запуск отсутствующей модели требует интернет. Дальнейшее распознавание
использует локальный кэш.

### Перевод не включается или первый запуск занимает много времени

- Включите **Голосовое переключение перевода на английский** в настройках.
- Произнесите «переведи на английский» или “translate to English” отдельной
  фразой. После включения в оверлее записи появится **→ EN**.
- `turbo` не поддерживает перевод. Pressay использует выбранную модель
  `small`, `medium` или `large-v3`; отсутствующая в кэше модель один раз
  скачивается перед использованием.
- Чтобы вернуться к обычному распознаванию, произнесите отдельной фразой
  «хватит переводить» или “stop translating”.

### Ограничения macOS beta

- Разрешите Microphone, Accessibility и Input Monitoring; после изменения
  Accessibility или Input Monitoring полностью перезапустите Pressay.
- Сочетания пока фиксированы: `Control+Option`, `Control+Option+Space`,
  `Control+Option+V` и `Control+Option+C`. Поля настройки сочетаний относятся к
  Windows.
- Распознавание работает на CPU: faster-whisper/CTranslate2 не использует
  Metal/MPS.
- CI проверяет импорты, state machines, скрипты и тесты. Микрофон, разрешения,
  глобальные клавиши и вставку нужно проверить на реальном Mac по
  [аппаратному чек-листу](TESTING.md#real-mac-acceptance-checklist--чек-лист-на-реальном-mac).
