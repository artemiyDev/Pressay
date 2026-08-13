# Pressay — руководство на русском

[English](README.en.md) · [Статус тестирования](TESTING.md) · [Главная](../README.md)

**Press → say.** Pressay — локальная голосовая диктовка для Windows и macOS.
После загрузки модели распознавание работает без облачного API. Поддерживаются
русский и английский языки.

## Возможности

- удержание горячих клавиш для push-to-talk и отдельный hands-free режим;
- локальное распознавание через faster-whisper;
- личный словарь и замены технических терминов;
- проверка активного окна и редактируемого поля перед каждой вставкой;
- последние две расшифровки только в памяти приложения;
- явное копирование без скрытой подмены clipboard при ошибках вставки.

## Windows 11 — стабильная версия

Требования: Windows 11 x64, Python 3.11, работающий микрофон. NVIDIA GPU
необязательна: при наличии CUDA используется GPU, иначе включается CPU.

```powershell
git clone https://github.com/artemiyDev/Pressay.git
cd Pressay
.\scripts\install.ps1 -DesktopShortcut -EnableAutostart
```

Установщик без прав администратора:

- создаёт `%LOCALAPPDATA%\Pressay\venv`;
- устанавливает зависимости и загружает модель `turbo`;
- создаёт Pressay в меню «Пуск» и, по запросу, на рабочем столе;
- по флагу `-EnableAutostart` создаёт ярлык в Startup;
- запускает приложение в системном трее.

Полезные варианты:

```powershell
.\scripts\install.ps1 -NoLaunch
.\scripts\install.ps1 -Model small
.\scripts\install.ps1 -SkipModel
.\scripts\doctor.ps1
.\scripts\test.ps1 -q
```

Горячие клавиши Windows:

| Действие | Клавиши |
|---|---|
| Диктовка при удержании | `Ctrl+Win` |
| Включить/выключить hands-free | `Ctrl+Win+Space` |
| Отмена | `Esc` |
| Вставить последнюю расшифровку | `Shift+Alt+Z` |
| Скопировать последнюю расшифровку | `Shift+Alt+X` |

## macOS 13+ — developer beta

Требования: Intel Mac или Apple Silicon, macOS 13+, Python 3.11. Эта версия
проверяется на GitHub-hosted Apple Silicon runner, но ещё не прошла ручной
аппаратный тест на реальном пользовательском Mac.

```bash
git clone https://github.com/artemiyDev/Pressay.git
cd Pressay
bash scripts/install-macos.sh
```

По умолчанию устанавливается CPU-модель `small`: она разумнее для первого
запуска без CUDA. Faster-whisper/CTranslate2 на Mac не использует Metal/MPS.

Установщик:

- создаёт `~/Library/Application Support/Pressay/venv`;
- устанавливает PySide6, faster-whisper, sounddevice и PyObjC;
- загружает и прогревает модель `small` на CPU;
- создаёт `~/Applications/Pressay.app` с новым логотипом;
- не включает автозапуск без явного `--enable-autostart`.

```bash
bash scripts/install-macos.sh --enable-autostart
bash scripts/install-macos.sh --model medium
bash scripts/install-macos.sh --no-launch
bash scripts/doctor-macos.sh
bash scripts/test-macos.sh
```

### Разрешения macOS

Откройте **System Settings → Privacy & Security** и разрешите Pressay:

1. **Microphone** — запись речи;
2. **Accessibility** — проверка редактируемого поля и Unicode-ввод;
3. **Input Monitoring** — глобальные горячие клавиши.

После изменения Accessibility или Input Monitoring полностью закройте и снова
запустите Pressay. Если macOS показывает Python вместо Pressay, разрешите тот
Python из `~/Library/Application Support/Pressay/venv/bin/python` — это
ограничение исходной beta-сборки. Подписанный `.dmg` станет отдельным release-этапом.

Горячие клавиши macOS:

| Действие | Клавиши |
|---|---|
| Диктовка при удержании | `Control+Option` |
| Включить/выключить hands-free | `Control+Option+Space` |
| Отмена | `Esc` |
| Вставить последнюю расшифровку | `Control+Option+V` |
| Скопировать последнюю расшифровку | `Control+Option+C` |

## Удаление

Windows:

```powershell
.\scripts\uninstall.ps1
.\scripts\uninstall.ps1 -RemoveRuntime
.\scripts\uninstall.ps1 -RemoveRuntime -RemoveUserData
```

macOS:

```bash
bash scripts/uninstall-macos.sh
bash scripts/uninstall-macos.sh --remove-runtime
bash scripts/uninstall-macos.sh --remove-user-data
```

Без destructive-флагов конфигурация и общий кэш моделей сохраняются.

## Где хранятся данные

| Данные | Windows | macOS |
|---|---|---|
| Конфигурация | `%LOCALAPPDATA%\Pressay\config.json` | `~/Library/Application Support/Pressay/config.json` |
| Runtime | `%LOCALAPPDATA%\Pressay\venv` | `~/Library/Application Support/Pressay/venv` |
| Логи | `%LOCALAPPDATA%\Pressay\pressay.log` | `~/Library/Application Support/Pressay/pressay.log` |
| Модели | Hugging Face cache | Hugging Face cache |

Логи не содержат текста расшифровок, аудио и заголовков окон.
