"""Small, focus-safe Qt user interface for Pressay."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QCloseEvent,
    QFont,
    QGuiApplication,
    QIcon,
    QPainter,
    QPalette,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import hotkey_bindings
from .audio import SILENCE_RMS_THRESHOLD
from .platform_support import platform_label
from .text import replacement_key


STATE_COLORS = {
    "idle": "#64748b",
    "ready": "#22c55e",
    "recording": "#ef4444",
    "processing": "#8b5cf6",
    "success": "#22c55e",
    "warning": "#f59e0b",
    "error": "#ef4444",
}
# Same keys as STATE_COLORS, but brightened where the light-mode accent reads
# below WCAG AA (4.5:1) against the dark status-card background (#1e293b).
STATE_COLORS_DARK = {
    "idle": "#94a3b8",
    "ready": "#22c55e",
    "recording": "#f87171",
    "processing": "#a78bfa",
    "success": "#22c55e",
    "warning": "#f59e0b",
    "error": "#f87171",
}
RECORDING_LEVEL_SILENCE_RMS = SILENCE_RMS_THRESHOLD
RECORDING_LEVEL_ACTIVE_COLOR = "#4ade80"
RECORDING_LEVEL_QUIET_COLOR = "#64748b"
ASSET_DIRECTORY = Path(__file__).with_name("assets")
APP_ICON_PATH = ASSET_DIRECTORY / "app-icon.svg"
_TRANSCRIPT_DISPLAY_LIMIT = 120


class _TranscriptHistoryList(QListWidget):
    """History list with the legacy text-readout method kept for callers."""

    def toPlainText(self) -> str:  # noqa: N802 - Qt compatibility spelling
        labels = ("Последняя", "Предыдущая")
        return "\n\n".join(
            f"{labels[index]}:\n{self.item(index).data(Qt.ItemDataRole.UserRole)}"
            for index in range(min(self.count(), len(labels)))
        )


# Color tokens for the pieces of SettingsWindow that used to hardcode light
# colors. Light values are the original literals verbatim so the light look
# stays pixel-identical.
LIGHT_THEME = {
    "status_bg": "#f1f5f9",
    "status_text": "#0f172a",
    "subtitle_text": "#64748b",
    "privacy_bg": "#ecfdf5",
    "privacy_text": "#475569",
}
DARK_THEME = {
    "status_bg": "#1e293b",
    "status_text": "#f1f5f9",
    "subtitle_text": "#94a3b8",
    "privacy_bg": "#0f172a",
    "privacy_text": "#e2e8f0",
}


def theme_tokens(scheme: Qt.ColorScheme) -> dict[str, str]:
    """Pure lookup: color tokens for the given Qt color scheme."""

    return DARK_THEME if scheme == Qt.ColorScheme.Dark else LIGHT_THEME


def state_colors_for_scheme(scheme: Qt.ColorScheme) -> dict[str, str]:
    """Pure lookup: state accent colors tuned for readability on the given scheme."""

    return STATE_COLORS_DARK if scheme == Qt.ColorScheme.Dark else STATE_COLORS


def detect_color_scheme() -> Qt.ColorScheme:
    """Detect the active Windows/Qt color scheme.

    Defensive by design: ``colorScheme()`` is missing on some Qt 6 builds and
    there may be no QApplication yet when this runs, so both cases fall back
    to reading the lightness of the current palette's window color.
    """

    if QApplication.instance() is None:
        return Qt.ColorScheme.Light
    try:
        style_hints = QGuiApplication.styleHints()
        color_scheme = getattr(style_hints, "colorScheme", None)
        if color_scheme is not None:
            scheme = color_scheme()
            if scheme != Qt.ColorScheme.Unknown:
                return scheme
    except Exception:
        pass
    window_color = QApplication.palette().color(QPalette.ColorRole.Window)
    return Qt.ColorScheme.Dark if window_color.lightness() < 128 else Qt.ColorScheme.Light


def format_replacements(replacements: dict[str, str]) -> str:
    """Render personal dictionary rules without exposing a JSON editor."""

    return "\n".join(f"{alias} = {canonical}" for alias, canonical in replacements.items())


def parse_replacements(text: str) -> dict[str, str]:
    """Parse one literal ``spoken form = canonical form`` rule per line."""

    result: dict[str, str] = {}
    seen: set[str] = set()
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"Строка {line_number}: нужен знак =")
        alias, canonical = (part.strip() for part in line.split("=", 1))
        if not alias or not canonical:
            raise ValueError(f"Строка {line_number}: обе части должны быть заполнены")
        folded = replacement_key(alias)
        if not folded:
            raise ValueError(f"Строка {line_number}: вариант «{alias}» становится пустым")
        if folded in seen:
            raise ValueError(f"Строка {line_number}: повторяется вариант «{alias}»")
        seen.add(folded)
        result[alias] = canonical
    return result


@dataclass(slots=True)
class MicrophoneChoice:
    value: str | None
    name: str
    legacy_index: int | None = None
    device_name: str | None = None
    is_default: bool = False

    @property
    def index(self) -> str | None:
        """Compatibility alias for callers from the index-based UI."""

        return self.value


def microphone_choice_index(
    microphones: list[MicrophoneChoice], selected: object
) -> int:
    """Find a stable choice, also recognizing legacy index/name settings."""

    for index, microphone in enumerate(microphones):
        if microphone.value == selected:
            return index

    legacy_index: int | None = None
    if type(selected) is int:
        legacy_index = selected
    elif isinstance(selected, str) and selected.isdecimal():
        legacy_index = int(selected)
    if legacy_index is not None:
        for index, microphone in enumerate(microphones):
            if microphone.legacy_index == legacy_index:
                return index

    if isinstance(selected, str):
        wanted_name = selected.strip().casefold()
        matches = [
            (index, microphone)
            for index, microphone in enumerate(microphones)
            if (
                microphone.device_name is not None
                and microphone.device_name.strip().casefold() == wanted_name
            )
        ]
        if matches:
            return min(matches, key=lambda item: (not item[1].is_default, item[0]))[0]
    return 0


class UiSignals(QObject):
    toggle_requested = Signal()
    cancel_requested = Signal()
    save_requested = Signal(dict)
    microphone_test_requested = Signal()
    paste_last_requested = Signal()
    copy_last_requested = Signal()
    quit_requested = Signal()


def make_icon(color: str | None = None, size: int = 64) -> QIcon:
    """Load the Pressay artwork and optionally add a small state indicator."""

    source = QIcon(str(APP_ICON_PATH))
    pixmap = source.pixmap(size, size)
    if pixmap.isNull():
        pixmap = QPixmap(size, size)
        pixmap.fill(QColor("#081a3a"))
    if color is None:
        return QIcon(pixmap)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QColor("#ffffff"))
    painter.setBrush(QColor(color))
    diameter = max(10, size // 4)
    painter.drawEllipse(size - diameter - 2, size - diameter - 2, diameter, diameter)
    painter.end()
    return QIcon(pixmap)


def recording_level_fraction(rms: float) -> float:
    """Map microphone RMS to a stable, perceptible overlay width."""

    if not math.isfinite(rms) or rms <= 0.0:
        return 0.0
    decibels = 20.0 * math.log10(rms)
    return max(0.0, min(1.0, (decibels + 65.0) / 50.0))


class StatusOverlay(QWidget):
    """A bottom-centre overlay that never steals keyboard focus."""

    def __init__(self, level_provider: Callable[[], float] | None = None) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        self._frame = QFrame(self)
        self._frame.setObjectName("overlayFrame")
        frame_layout = QVBoxLayout(self._frame)
        frame_layout.setContentsMargins(16, 9, 16, 9)
        frame_layout.setSpacing(6)
        content_layout = QHBoxLayout()
        content_layout.setSpacing(9)
        self._dot = QLabel("●")
        self._dot.setFont(QFont("Segoe UI", 13))
        self._label = QLabel("Готов")
        self._label.setFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold))
        content_layout.addWidget(self._dot)
        content_layout.addWidget(self._label)
        frame_layout.addLayout(content_layout)
        self._level_track = QFrame()
        self._level_track.setFixedHeight(5)
        level_layout = QHBoxLayout(self._level_track)
        level_layout.setContentsMargins(0, 0, 0, 0)
        level_layout.setSpacing(0)
        self._level_fill = QFrame()
        self._level_fill.setFixedHeight(5)
        level_layout.addWidget(self._level_fill)
        level_layout.addStretch()
        self._level_track.hide()
        frame_layout.addWidget(self._level_track)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._frame)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)
        self._level_provider = level_provider
        self._level_fraction = 0.0
        self._level_timer = QTimer(self)
        self._level_timer.setInterval(50)
        self._level_timer.timeout.connect(self._refresh_level)
        # The overlay plate is always dark, in either system theme, so it uses
        # the accents tuned for dark backgrounds rather than the window ones.
        self._apply_color(STATE_COLORS_DARK["ready"])

    def _apply_color(self, color: str) -> None:
        self._frame.setStyleSheet(
            "QFrame#overlayFrame {"
            "background: rgba(17, 24, 39, 235);"
            "border: 1px solid rgba(255, 255, 255, 32);"
            "border-radius: 15px;"
            "}"
            "QLabel { color: #f8fafc; }"
        )
        self._dot.setStyleSheet(f"color: {color};")

    def _set_level(self, rms: float) -> None:
        self._level_fraction = recording_level_fraction(rms)
        width = int(self._level_track.contentsRect().width() * self._level_fraction)
        if self._level_fraction > 0.0:
            width = max(1, width)
        self._level_fill.setFixedWidth(width)
        color = (
            RECORDING_LEVEL_ACTIVE_COLOR
            if rms >= RECORDING_LEVEL_SILENCE_RMS
            else RECORDING_LEVEL_QUIET_COLOR
        )
        self._level_fill.setStyleSheet(
            f"background: {color}; border-radius: 2px;"
        )

    def _refresh_level(self) -> None:
        if not self._level_track.isVisible():
            return
        try:
            rms = float(self._level_provider()) if self._level_provider is not None else 0.0
        except (TypeError, ValueError):
            rms = 0.0
        self._set_level(rms)

    def hideEvent(self, event: Any) -> None:
        self._level_timer.stop()
        super().hideEvent(event)

    def show_status(self, text: str, state: str, *, auto_hide_ms: int = 0) -> None:
        self._hide_timer.stop()
        self._level_timer.stop()
        self._label.setText(text)
        self._apply_color(STATE_COLORS_DARK.get(state, STATE_COLORS_DARK["idle"]))
        recording = state == "recording"
        self._level_track.setVisible(recording)
        self.adjustSize()
        screen = QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.move(
                QPoint(
                    area.center().x() - self.width() // 2,
                    area.bottom() - self.height() - 38,
                )
            )
        self.show()
        self.raise_()
        if recording:
            self._refresh_level()
            self._level_timer.start()
        if auto_hide_ms:
            self._hide_timer.start(auto_hide_ms)


class SettingsWindow(QMainWindow):
    def __init__(
        self,
        signals: UiSignals,
        settings: dict[str, Any],
        microphones: list[MicrophoneChoice],
    ) -> None:
        super().__init__()
        self.signals = signals
        self.setWindowTitle("Pressay")
        self.setWindowIcon(make_icon())
        self.setMinimumWidth(560)
        self.resize(620, 790)
        self._really_close = False
        self._recent_transcripts: deque[str] = deque(maxlen=20)
        # Remembered so a later theme switch can redraw the status card
        # without a second, competing definition of "what the status is".
        self._status_text = "Готов к диктовке"
        self._status_state: str | None = None
        # Muted explanatory lines; recolored together on a theme switch.
        self._hint_labels: list[QLabel] = []
        self._color_scheme = detect_color_scheme()

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(16)

        brand_row = QHBoxLayout()
        brand_icon = QLabel()
        brand_icon.setPixmap(make_icon(size=64).pixmap(64, 64))
        brand_icon.setFixedSize(64, 64)
        brand_text = QVBoxLayout()
        title = QLabel("Pressay")
        title.setFont(QFont("Segoe UI", 22, QFont.Weight.Bold))
        self._subtitle_label = QLabel(f"Локальная диктовка в любом приложении {platform_label()}")
        brand_text.addWidget(title)
        brand_text.addWidget(self._subtitle_label)
        brand_row.addWidget(brand_icon)
        brand_row.addLayout(brand_text)
        brand_row.addStretch(1)
        layout.addLayout(brand_row)

        self.status_label = QLabel(self._status_text)
        self.status_label.setObjectName("statusCard")
        layout.addWidget(self.status_label)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setVerticalSpacing(12)

        self.microphone_combo = QComboBox()
        for microphone in microphones:
            self.microphone_combo.addItem(microphone.name, microphone.value)
        selected_mic = settings.get("microphone")
        selected_index = microphone_choice_index(microphones, selected_mic)
        self.microphone_combo.setCurrentIndex(selected_index)
        form.addRow("Микрофон", self.microphone_combo)

        self.language_combo = QComboBox()
        self.language_combo.addItem("Автоматически (RU/EN)", "auto")
        self.language_combo.addItem("Русский", "ru")
        self.language_combo.addItem("English", "en")
        language_index = self.language_combo.findData(settings.get("language", "auto"))
        self.language_combo.setCurrentIndex(max(0, language_index))
        form.addRow("Язык", self.language_combo)
        language_hint = QLabel(
            "Постоянный язык примерно на треть быстрее: определение языка "
            "пропускается. Но речь на другом языке будет не распознана, "
            "а переведена — без предупреждения."
        )
        language_hint.setWordWrap(True)
        self._hint_labels.append(language_hint)
        form.addRow("", language_hint)

        self.model_combo = QComboBox()
        for label, value in (
            ("Small — быстрый старт (~0.5 ГБ)", "small"),
            ("Medium — выше качество (~1.5 ГБ)", "medium"),
            ("Turbo — рекомендован для RTX (~1.5 ГБ)", "turbo"),
            ("Large v3 — максимум качества (~3 ГБ)", "large-v3"),
        ):
            self.model_combo.addItem(label, value)
        model_index = self.model_combo.findData(settings.get("model", "turbo"))
        self.model_combo.setCurrentIndex(max(0, model_index))
        form.addRow("Модель", self.model_combo)
        self.active_model_label = QLabel("Модель ещё не загружалась")
        self._hint_labels.append(self.active_model_label)
        form.addRow("", self.active_model_label)

        self.resource_mode_combo = QComboBox()
        for label, value in (
            ("Мгновенно — модель всегда в GPU", "instant"),
            ("Сбалансированно — выгрузить через 5 минут", "balanced"),
            ("Экономно — выгружать после каждой фразы", "eco"),
        ):
            self.resource_mode_combo.addItem(label, value)
        resource_index = self.resource_mode_combo.findData(
            settings.get("resource_mode", "instant")
        )
        self.resource_mode_combo.setCurrentIndex(max(0, resource_index))
        form.addRow("Ресурсы", self.resource_mode_combo)
        layout.addLayout(form)

        self.auto_insert_checkbox = QCheckBox("Автоматически вставлять в исходное окно")
        self.auto_insert_checkbox.setChecked(bool(settings.get("auto_insert", True)))
        self.smart_spacing_checkbox = QCheckBox("Добавлять пробел между диктовками")
        self.smart_spacing_checkbox.setChecked(bool(settings.get("smart_spacing", True)))
        self.remove_fillers_checkbox = QCheckBox("Удалять явные слова-паразиты (опционально)")
        self.remove_fillers_checkbox.setChecked(bool(settings.get("remove_fillers", False)))
        self.press_enter_checkbox = QCheckBox('Разрешить голосовую команду «нажми Enter»')
        self.press_enter_checkbox.setChecked(bool(settings.get("press_enter", False)))
        self.voice_formatting_checkbox = QCheckBox(
            'Голосовые команды форматирования: «с новой строки», «абзац»'
        )
        self.voice_formatting_checkbox.setChecked(bool(settings.get("voice_formatting", False)))
        layout.addWidget(self.auto_insert_checkbox)
        layout.addWidget(self.smart_spacing_checkbox)
        layout.addWidget(self.remove_fillers_checkbox)
        layout.addWidget(self.press_enter_checkbox)
        layout.addWidget(self.voice_formatting_checkbox)

        hotkeys_defaults = hotkey_bindings.HotkeyBindings().to_mapping()
        hotkeys_settings = settings.get("hotkeys", hotkeys_defaults)

        hotkeys_label = QLabel("Горячие клавиши")
        hotkeys_label.setStyleSheet("font-weight: 600;")
        layout.addWidget(hotkeys_label)

        hotkeys_form = QFormLayout()
        hotkeys_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        hotkeys_form.setVerticalSpacing(12)

        self.hold_modifiers_combo = QComboBox()
        for pair in hotkey_bindings.HOLD_MODIFIER_PAIRS:
            canonical = "+".join(pair)
            label = "+".join(part.capitalize() for part in pair)
            self.hold_modifiers_combo.addItem(label, canonical)
        hold_index = self.hold_modifiers_combo.findData(
            hotkeys_settings.get("hold_modifiers", hotkeys_defaults["hold_modifiers"])
        )
        self.hold_modifiers_combo.setCurrentIndex(max(0, hold_index))
        hotkeys_form.addRow("Комбинация удержания", self.hold_modifiers_combo)

        self.push_to_talk_checkbox = QCheckBox(
            "Push-to-talk (при выключении запись включается и выключается только "
            "сочетанием с клавишей переключения, без удержания)"
        )
        self.push_to_talk_checkbox.setChecked(
            bool(hotkeys_settings.get("push_to_talk", hotkeys_defaults["push_to_talk"]))
        )
        hotkeys_form.addRow(self.push_to_talk_checkbox)

        self.toggle_key_edit = QLineEdit(
            str(hotkeys_settings.get("toggle_key", hotkeys_defaults["toggle_key"]))
        )
        hotkeys_form.addRow("Клавиша переключения", self.toggle_key_edit)

        self.paste_last_edit = QLineEdit(
            str(hotkeys_settings.get("paste_last", hotkeys_defaults["paste_last"]))
        )
        hotkeys_form.addRow("Вставить последнюю", self.paste_last_edit)

        self.copy_last_edit = QLineEdit(
            str(hotkeys_settings.get("copy_last", hotkeys_defaults["copy_last"]))
        )
        hotkeys_form.addRow("Скопировать", self.copy_last_edit)
        layout.addLayout(hotkeys_form)

        hotkeys_format_label = QLabel(
            "Части сочетания пишутся через «+»: модификаторы ctrl, win, shift, alt "
            "и одна обычная клавиша — буква, цифра, space или f1–f12. Слово none "
            "отключает действие."
        )
        hotkeys_format_label.setWordWrap(True)
        self._hint_labels.append(hotkeys_format_label)
        layout.addWidget(hotkeys_format_label)

        hotkeys_conflict_label = QLabel(
            "Внимание, возможные конфликты: Ctrl+Alt на многих раскладках равносилен "
            "AltGr и используется для ввода символов; Ctrl+Shift и Shift+Alt — "
            "стандартные сочетания переключения раскладки Windows; Ctrl+Win "
            "(по умолчанию) конфликтов не имеет."
        )
        hotkeys_conflict_label.setWordWrap(True)
        self._hint_labels.append(hotkeys_conflict_label)
        layout.addWidget(hotkeys_conflict_label)

        dictionary_label = QLabel("Личный словарь: произношение = правильное написание")
        dictionary_label.setStyleSheet("font-weight: 600;")
        self.dictionary_edit = QTextEdit()
        self.dictionary_edit.setPlaceholderText(
            "фаст апи = FastAPI\nдокер композ = Docker Compose"
        )
        self.dictionary_edit.setPlainText(
            format_replacements(dict(settings.get("replacements", {})))
        )
        self.dictionary_edit.setMaximumHeight(90)
        layout.addWidget(dictionary_label)
        layout.addWidget(self.dictionary_edit)

        actions = QHBoxLayout()
        self.toggle_button = QPushButton("Тестовая диктовка")
        self.toggle_button.setDefault(True)
        self.toggle_button.clicked.connect(signals.toggle_requested)
        self.test_button = QPushButton("Проверить микрофон")
        self.test_button.clicked.connect(signals.microphone_test_requested)
        save_button = QPushButton("Сохранить")
        save_button.clicked.connect(self._emit_save)
        actions.addWidget(self.toggle_button)
        actions.addWidget(self.test_button)
        actions.addStretch(1)
        actions.addWidget(save_button)
        layout.addLayout(actions)

        history_label = QLabel("История расшифровок")
        history_label.setStyleSheet("font-weight: 600;")
        history_hint = QLabel("До 20 записей, только в памяти до закрытия приложения.")
        history_hint.setWordWrap(True)
        self._hint_labels.append(history_hint)
        self.last_transcript = _TranscriptHistoryList()
        self.last_transcript.setMaximumHeight(190)
        self.last_transcript.itemDoubleClicked.connect(self._copy_transcript_item)
        layout.addWidget(history_label)
        layout.addWidget(history_hint)
        layout.addWidget(self.last_transcript)

        last_actions = QHBoxLayout()
        paste_button = QPushButton("Вставить последнюю")
        paste_button.clicked.connect(signals.paste_last_requested)
        self.copy_transcript_button = QPushButton("Копировать")
        self.copy_transcript_button.setEnabled(False)
        self.copy_transcript_button.clicked.connect(self._copy_selected_transcript)
        self.clear_transcript_history_button = QPushButton("Очистить историю")
        self.clear_transcript_history_button.clicked.connect(self._clear_transcript_history)
        last_actions.addWidget(paste_button)
        last_actions.addWidget(self.copy_transcript_button)
        last_actions.addWidget(self.clear_transcript_history_button)
        last_actions.addStretch(1)
        layout.addLayout(last_actions)

        self._privacy_label = QLabel(
            "🔒 Аудио не покидает компьютер и не сохраняется на диск. "
            "Сеть нужна только для первой загрузки модели."
        )
        self._privacy_label.setWordWrap(True)
        layout.addWidget(self._privacy_label)

        self.setCentralWidget(root)
        self._apply_theme(self._color_scheme)

        # The style hints singleton outlives this window; PySide auto-drops
        # the connection once this QObject (the receiver) is destroyed, so
        # there is nothing to disconnect by hand.
        style_hints = QGuiApplication.styleHints()
        color_scheme_changed = getattr(style_hints, "colorSchemeChanged", None)
        if color_scheme_changed is not None:
            color_scheme_changed.connect(self._on_color_scheme_changed)

    def current_settings(self) -> dict[str, Any]:
        return {
            "microphone": self.microphone_combo.currentData(),
            "language": self.language_combo.currentData(),
            "model": self.model_combo.currentData(),
            "resource_mode": self.resource_mode_combo.currentData(),
            "auto_insert": self.auto_insert_checkbox.isChecked(),
            "smart_spacing": self.smart_spacing_checkbox.isChecked(),
            "remove_fillers": self.remove_fillers_checkbox.isChecked(),
            "press_enter": self.press_enter_checkbox.isChecked(),
            "voice_formatting": self.voice_formatting_checkbox.isChecked(),
            "replacements": parse_replacements(self.dictionary_edit.toPlainText()),
            "hotkeys": self._current_hotkeys(),
        }

    def _current_hotkeys(self) -> dict[str, Any]:
        raw = {
            "hold_modifiers": self.hold_modifiers_combo.currentData(),
            "toggle_key": self.toggle_key_edit.text(),
            "paste_last": self.paste_last_edit.text(),
            "copy_last": self.copy_last_edit.text(),
            "push_to_talk": self.push_to_talk_checkbox.isChecked(),
        }
        try:
            bindings = hotkey_bindings.from_mapping(raw)
        except hotkey_bindings.HotkeyBindingError as exc:
            raise ValueError(f"Горячие клавиши: {exc}") from exc
        return bindings.to_mapping()

    def _emit_save(self) -> None:
        try:
            settings = self.current_settings()
        except ValueError as exc:
            QMessageBox.warning(self, "Ошибка настроек", str(exc))
            return
        self.signals.save_requested.emit(settings)

    def update_status(self, text: str, state: str = "idle") -> None:
        self._status_text = text
        self._status_state = state
        self.status_label.setText(text)
        self._restyle_status()
        self.toggle_button.setText("Завершить тест" if state == "recording" else "Тестовая диктовка")

    def update_active_model(self, model: str, device: str, compute_type: str) -> None:
        """Show the backend that successfully completed model warmup."""

        self.active_model_label.setText(
            f"Активна: {model} · {device.upper()} · {compute_type}"
        )

    def _restyle_status(self) -> None:
        """Apply the status-card stylesheet for the current text/state and theme.

        The one spot that turns (state, scheme) into a stylesheet, so a
        theme switch just needs to call this again with the remembered
        state instead of duplicating the color logic.
        """

        tokens = theme_tokens(self._color_scheme)
        if self._status_state is None:
            # Initial "untouched" look: plain readable text, no state accent.
            self.status_label.setStyleSheet(
                f"QLabel#statusCard {{ background: {tokens['status_bg']}; color: {tokens['status_text']};"
                " border-radius: 10px; padding: 13px; font-weight: 600; }"
            )
            return
        accents = state_colors_for_scheme(self._color_scheme)
        color = accents.get(self._status_state, accents["idle"])
        self.status_label.setStyleSheet(
            f"QLabel#statusCard {{ background: {tokens['status_bg']}; color: {color};"
            " border-radius: 10px; padding: 13px; font-weight: 700; }"
        )

    def _apply_theme(self, scheme: Qt.ColorScheme) -> None:
        """Recolor every themed widget in this window for ``scheme``."""

        self._color_scheme = scheme
        tokens = theme_tokens(scheme)
        self._subtitle_label.setStyleSheet(f"color: {tokens['subtitle_text']};")
        for hint in self._hint_labels:
            hint.setStyleSheet(f"color: {tokens['subtitle_text']};")
        self._privacy_label.setStyleSheet(
            f"color: {tokens['privacy_text']}; background: {tokens['privacy_bg']};"
            " padding: 10px; border-radius: 8px;"
        )
        self._restyle_status()

    def _on_color_scheme_changed(self, scheme: Qt.ColorScheme) -> None:
        if scheme == Qt.ColorScheme.Unknown:
            scheme = detect_color_scheme()
        self._apply_theme(scheme)

    def set_last_transcript(self, text: str) -> None:
        if not text:
            return
        if self._recent_transcripts and self._recent_transcripts[0] == text:
            return
        self._recent_transcripts.appendleft(text)
        self._render_transcript_history()

    def _render_transcript_history(self) -> None:
        self.last_transcript.clear()
        for transcript in self._recent_transcripts:
            item = QListWidgetItem(self._display_transcript(transcript))
            item.setData(Qt.ItemDataRole.UserRole, transcript)
            item.setToolTip(transcript)
            self.last_transcript.addItem(item)
        if self._recent_transcripts:
            self.last_transcript.setCurrentRow(0)
        self.copy_transcript_button.setEnabled(bool(self._recent_transcripts))

    @staticmethod
    def _display_transcript(text: str) -> str:
        if len(text) <= _TRANSCRIPT_DISPLAY_LIMIT:
            return text
        return f"{text[:_TRANSCRIPT_DISPLAY_LIMIT].rstrip()}…"

    def _copy_selected_transcript(self) -> None:
        item = self.last_transcript.currentItem()
        if item is not None:
            self._copy_transcript_item(item)

    def _copy_transcript_item(self, item: QListWidgetItem) -> None:
        QApplication.clipboard().setText(str(item.data(Qt.ItemDataRole.UserRole)))
        self.update_status("Скопировано", "success")

    def _clear_transcript_history(self) -> None:
        self._recent_transcripts.clear()
        self.last_transcript.clear()
        self.copy_transcript_button.setEnabled(False)

    def confirm(self, title: str, text: str) -> None:
        QMessageBox.information(self, title, text)

    def prepare_to_quit(self) -> None:
        self._really_close = True

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if self._really_close:
            event.accept()
        else:
            event.ignore()
            self.hide()


class TrayController(QObject):
    def __init__(self, signals: UiSignals, window: SettingsWindow) -> None:
        super().__init__(window)
        self.signals = signals
        self.window = window
        self.tray = QSystemTrayIcon(make_icon(STATE_COLORS["ready"]), self)
        self.tray.setToolTip("Pressay — готов")

        menu = QMenu()
        open_action = QAction("Открыть Pressay", menu)
        open_action.triggered.connect(self.show_window)
        toggle_action = QAction("Начать / завершить диктовку", menu)
        toggle_action.triggered.connect(signals.toggle_requested)
        paste_action = QAction("Вставить последнюю расшифровку", menu)
        paste_action.triggered.connect(signals.paste_last_requested)
        copy_action = QAction("Копировать последнюю расшифровку", menu)
        copy_action.triggered.connect(signals.copy_last_requested)
        quit_action = QAction("Выход", menu)
        quit_action.triggered.connect(signals.quit_requested)
        menu.addAction(open_action)
        menu.addSeparator()
        menu.addAction(toggle_action)
        menu.addAction(paste_action)
        menu.addAction(copy_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._activated)
        self.tray.show()

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.DoubleClick,
            QSystemTrayIcon.ActivationReason.Trigger,
        ):
            self.show_window()

    def show_window(self) -> None:
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def update_state(self, text: str, state: str) -> None:
        self.tray.setIcon(make_icon(STATE_COLORS.get(state, STATE_COLORS["idle"])))
        self.tray.setToolTip(f"Pressay — {text}")

    def notify(self, title: str, message: str, *, warning: bool = False) -> None:
        icon = QSystemTrayIcon.MessageIcon.Warning if warning else QSystemTrayIcon.MessageIcon.Information
        self.tray.showMessage(title, message, icon, 5000)
