"""Small, focus-safe Qt user interface for Pressay."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QCloseEvent, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .platform_support import platform_label


STATE_COLORS = {
    "idle": "#64748b",
    "ready": "#22c55e",
    "recording": "#ef4444",
    "processing": "#8b5cf6",
    "success": "#22c55e",
    "warning": "#f59e0b",
    "error": "#ef4444",
}
ASSET_DIRECTORY = Path(__file__).with_name("assets")
APP_ICON_PATH = ASSET_DIRECTORY / "app-icon.svg"


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
        folded = alias.casefold()
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


class StatusOverlay(QWidget):
    """A bottom-centre overlay that never steals keyboard focus."""

    def __init__(self) -> None:
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
        frame_layout = QHBoxLayout(self._frame)
        frame_layout.setContentsMargins(16, 9, 16, 9)
        frame_layout.setSpacing(9)
        self._dot = QLabel("●")
        self._dot.setFont(QFont("Segoe UI", 13))
        self._label = QLabel("Готов")
        self._label.setFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold))
        frame_layout.addWidget(self._dot)
        frame_layout.addWidget(self._label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._frame)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)
        self._apply_color(STATE_COLORS["ready"])

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

    def show_status(self, text: str, state: str, *, auto_hide_ms: int = 0) -> None:
        self._hide_timer.stop()
        self._label.setText(text)
        self._apply_color(STATE_COLORS.get(state, STATE_COLORS["idle"]))
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
        self._recent_transcripts: deque[str] = deque(maxlen=2)

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
        subtitle = QLabel(f"Локальная диктовка в любом приложении {platform_label()}")
        subtitle.setStyleSheet("color: #64748b;")
        brand_text.addWidget(title)
        brand_text.addWidget(subtitle)
        brand_row.addWidget(brand_icon)
        brand_row.addLayout(brand_text)
        brand_row.addStretch(1)
        layout.addLayout(brand_row)

        self.status_label = QLabel("Готов к диктовке")
        self.status_label.setObjectName("statusCard")
        self.status_label.setStyleSheet(
            "QLabel#statusCard { background: #f1f5f9; color: #0f172a;"
            " border-radius: 10px; padding: 13px; font-weight: 600; }"
        )
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

        self.model_combo = QComboBox()
        for label, value in (
            ("Small — быстрый старт", "small"),
            ("Medium — выше качество", "medium"),
            ("Turbo — рекомендован для RTX", "turbo"),
            ("Large v3 — максимум качества", "large-v3"),
        ):
            self.model_combo.addItem(label, value)
        model_index = self.model_combo.findData(settings.get("model", "turbo"))
        self.model_combo.setCurrentIndex(max(0, model_index))
        form.addRow("Модель", self.model_combo)

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
        layout.addWidget(self.auto_insert_checkbox)
        layout.addWidget(self.smart_spacing_checkbox)
        layout.addWidget(self.remove_fillers_checkbox)
        layout.addWidget(self.press_enter_checkbox)

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

        last_label = QLabel("Последние две расшифровки (только в памяти)")
        last_label.setStyleSheet("font-weight: 600;")
        self.last_transcript = QTextEdit()
        self.last_transcript.setReadOnly(True)
        self.last_transcript.setPlaceholderText("Здесь появятся два последних результата")
        self.last_transcript.setMaximumHeight(155)
        layout.addWidget(last_label)
        layout.addWidget(self.last_transcript)

        last_actions = QHBoxLayout()
        paste_button = QPushButton("Вставить последнюю")
        paste_button.clicked.connect(signals.paste_last_requested)
        copy_button = QPushButton("Копировать")
        copy_button.clicked.connect(signals.copy_last_requested)
        last_actions.addWidget(paste_button)
        last_actions.addWidget(copy_button)
        last_actions.addStretch(1)
        layout.addLayout(last_actions)

        privacy = QLabel(
            "🔒 Аудио не покидает компьютер и не сохраняется на диск. "
            "Сеть нужна только для первой загрузки модели."
        )
        privacy.setWordWrap(True)
        privacy.setStyleSheet("color: #475569; background: #ecfdf5; padding: 10px; border-radius: 8px;")
        layout.addWidget(privacy)

        self.setCentralWidget(root)

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
            "replacements": parse_replacements(self.dictionary_edit.toPlainText()),
        }

    def _emit_save(self) -> None:
        try:
            settings = self.current_settings()
        except ValueError as exc:
            QMessageBox.warning(self, "Ошибка словаря", str(exc))
            return
        self.signals.save_requested.emit(settings)

    def update_status(self, text: str, state: str = "idle") -> None:
        self.status_label.setText(text)
        color = STATE_COLORS.get(state, STATE_COLORS["idle"])
        self.status_label.setStyleSheet(
            f"QLabel#statusCard {{ background: #f1f5f9; color: {color};"
            " border-radius: 10px; padding: 13px; font-weight: 700; }"
        )
        self.toggle_button.setText("Завершить тест" if state == "recording" else "Тестовая диктовка")

    def set_last_transcript(self, text: str) -> None:
        if not text:
            return
        if not self._recent_transcripts or self._recent_transcripts[0] != text:
            self._recent_transcripts.appendleft(text)
        labels = ("Последняя", "Предыдущая")
        rendered = [
            f"{labels[index]}:\n{value}"
            for index, value in enumerate(self._recent_transcripts)
        ]
        self.last_transcript.setPlainText("\n\n".join(rendered))

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
