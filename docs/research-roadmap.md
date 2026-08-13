# Pressay: архитектурные решения и roadmap

Решения основаны на сравнении локальных инструментов голосовой диктовки,
проверках на Windows и измерениях рабочего прототипа.

## Уже принято в текущей Python-версии

- final-after-release: целевое приложение получает только готовый результат;
- отдельные потоки для keyboard hook, UI, audio lifecycle и ASR;
- PTT управляет записью, VAD только проверяет/обрезает речь;
- постоянная прогретая Turbo-модель с CUDA-to-CPU fallback;
- ограничение длительности и памяти записи, контроль device loss/overflow;
- focus/control guard перед каждым этапом ввода;
- Unicode SendInput без зависимости от раскладки как основной проверенный путь;
- транзакционный clipboard только для явной вставки и сложного Unicode;
- structured logs без transcript/audio/window title;
- личный словарь: ASR bias плюс детерминированная canonicalization;
- режимы residency `instant`, `balanced`, `eco`;
- метрика `post_release_seconds` и отдельные ASR/finalization timings.

## Осознанные отличия от общей рекомендации

Clipboard-first пока не становится default. На этой машине Unicode SendInput
подтверждён в Chromium-приложениях, не меняет clipboard и не зависит от раскладки.
Clipboard path остаётся для multiline/non-BMP и явной команды, с sequence и
focus guards. Менять приоритет можно только после app-matrix benchmark без
потери rich clipboard formats.

Полная миграция на Rust/Tauri не выполняется поверх работающего продукта одним
шагом. Сначала стабилизируется контракт `SpeechEngine` и собираются собственные
latency/accuracy данные. Native host должен заменить Python hot path только с
измеримым выигрышем и сохранением всех privacy/focus invariants.

## Следующие этапы

1. Создать локальный RU/EN mixed dataset: сначала 100 проверенных фраз, затем
   600–1000; хранить literal и polished references отдельно.
2. Добавить benchmark runner для WER/CER, technical-term exact match и
   p50/p95 `post_release_seconds`; сравнить cold/warm Turbo CUDA и CPU INT8.
3. Выделить ASR в supervised worker process с request ID, heartbeat, timeout и
   deterministic termination — это даст crash isolation и гарантированный
   возврат VRAM.
4. Сделать Windows injection matrix: Win32 Edit/RichEdit, Chromium, VS Code,
   Terminal, Telegram, Qt, Word и elevated target. API success не считать
   доказательством видимой вставки.
5. Исследовать WASAPI event-driven host и device notifications вместо замены
   стабильного audio path без benchmark.
6. Добавить bounded context только для разрешённых editable controls:
   foreground app profile и малое окно около caret; password/sensitive fields
   всегда исключать.
7. App-scoped dictionary и явный correction flow внедрять до любого LLM.
8. Qwen3-ASR/Parakeet/whisper.cpp подключать только как engines за единым
   контрактом и принимать по собственному RU/EN code-switch benchmark.

## Acceptance для следующей архитектурной фазы

- 10 000 synthetic lifecycle cycles без orphan process и роста памяти;
- ноль wrong-window injections и clipboard overwrites;
- отдельные cold/warm p50/p95 для каждой модели и resource mode;
- worker crash не останавливает hotkeys/tray и даёт понятный local fallback;
- transcript/audio/context отсутствуют в логах и telemetry остаётся выключенной.
