# meikipop/gui/anki_save.py
import logging
import os
import tempfile
from typing import Dict, List, Optional

from PyQt6.QtCore import QPoint, Qt, pyqtSignal, QTimer
from PyQt6.QtGui import QCursor, QMouseEvent, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QPushButton,
    QComboBox, QTextEdit, QScrollArea, QWidget, QFileDialog, QFrame,
    QSizePolicy, QApplication,
)

from meikipop.config.config import config
from meikipop.dictionary.lookup import DictionaryEntry, KanjiEntry
from meikipop.gui.region_selector import RegionSelector
from meikipop.scripts.anki import AnkiConnect, AnkiConnectError

logger = logging.getLogger(__name__)

# Fields whose names (lowercased) map to an auto-fill source.
_WORD_KEYS    = {'front', 'word', 'expression', 'vocabulary', 'kanji', 'term'}
_READING_KEYS = {'back', 'reading', 'kana', 'furigana', 'pronunciation'}
_MEANING_KEYS = {'meaning', 'definition', 'gloss', 'translation', 'english'}
_SCREEN_KEYS  = {'screenshot', 'image', 'picture', 'screen', 'context'}

_FALLBACK_DECKS  = ["Default"]
_FALLBACK_MODELS = ["Basic"]
_FALLBACK_FIELDS: Dict[str, List[str]] = {
    "Basic": ["Front", "Back"],
    "Basic (and reversed card)": ["Front", "Back"],
}


def _with_config_default(default_value: Optional[str], fallback_values: List[str]) -> List[str]:
    """Return combo items with config default first, preserving fallback values."""
    items = list(fallback_values)
    if default_value and default_value not in items:
        items.insert(0, default_value)
    return items


def _first_gloss(entry: Optional[DictionaryEntry]) -> str:
    if not entry or not isinstance(entry, DictionaryEntry):
        return ""
    for sense in entry.senses:
        glosses = sense.get("glosses", [])
        if glosses:
            return glosses[0]
    return ""


def _kata_to_hira(text: str) -> str:
    res = []
    for c in text:
        code = ord(c)
        if 0x30A1 <= code <= 0x30F6:
            res.append(chr(code - 0x60))
        elif code == 0x30FD:  # ヽ -> ゝ
            res.append("\u309D")
        elif code == 0x30FE:  # ヾ -> ゞ
            res.append("\u309E")
        else:
            res.append(c)
    return "".join(res)


def _hotkey_to_pynput(hotkey_str: str) -> str:
    """Convert 'ctrl+shift+s' → '<ctrl>+<shift>+s' for pynput GlobalHotKeys."""
    modifiers = {'ctrl', 'shift', 'alt', 'cmd', 'super', 'meta', 'option'}
    parts = hotkey_str.lower().split('+')
    return '+'.join(f'<{p}>' if p in modifiers else p for p in parts)


class AnkiSaveDialog(QDialog):
    """Modal-free dialog for adding a word to Anki.

    Show/hide is driven by a hotkey listener that emits _show_signal on the
    main thread via Qt's signal/slot mechanism (thread-safe).
    """

    _show_signal = pyqtSignal()

    def __init__(self, popup_window, screen_manager, parent=None):
        super().__init__(parent)
        self.popup_window = popup_window
        self.screen_manager = screen_manager
        self._anki = AnkiConnect()
        self._screenshot_path: Optional[str] = None
        self._field_inputs: Dict[str, "ResizableTextEdit"] = {}
        self._field_value_cache: Dict[str, str] = {}
        self._screenshot_field_combo: Optional[QComboBox] = None
        self._screenshot_action_combo: Optional[QComboBox] = None
        self._hotkey_listener = None
        self._temp_screenshot: Optional[str] = None  # managed temp file

        self.setWindowTitle("Add to Anki")
        self.setMinimumWidth(520)
        self.setWindowFlags(
            Qt.WindowType.Dialog |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self._apply_stylesheet()
        self._build_ui()
        self._update_apply_button_state()

        self._show_signal.connect(self._on_hotkey_triggered)
        self._start_hotkey_listener()

    # ──────────────────────────────────────────────
    # Stylesheet
    # ──────────────────────────────────────────────

    def _apply_stylesheet(self):
        """Apply the stylesheet for the dialog and its child widgets."""
        self.setStyleSheet("""
            QDialog {
                background-color: #1e1e2e;
                color: #cdd6f4;
            }
            QLabel {
                color: #cdd6f4;
                background: transparent;
            }
            QLabel#section_label {
                color: #a6adc8;
                font-size: 11px;
                text-transform: uppercase;
                letter-spacing: 1px;
            }
            QLineEdit {
                background-color: #313244;
                color: #cdd6f4;
                border: 1px solid #45475a;
                border-radius: 4px;
                padding: 5px 8px;
                selection-background-color: #89b4fa;
            }
            QLineEdit:focus {
                border-color: #89b4fa;
            }
            QTextEdit {
                background-color: #313244;
                color: #cdd6f4;
                border: 1px solid #45475a;
                border-radius: 4px;
                padding: 4px 6px;
                selection-background-color: #89b4fa;
            }
            QTextEdit:focus {
                border-color: #89b4fa;
            }
            QComboBox {
                background-color: #313244;
                color: #cdd6f4;
                border: 1px solid #45475a;
                border-radius: 4px;
                padding: 4px 8px;
                min-width: 160px;
            }
            QComboBox::drop-down {
                border: none;
                padding-right: 6px;
            }
            QComboBox QAbstractItemView {
                background-color: #313244;
                color: #cdd6f4;
                selection-background-color: #45475a;
                border: 1px solid #45475a;
            }
            QPushButton {
                background-color: #313244;
                color: #cdd6f4;
                border: 1px solid #45475a;
                border-radius: 4px;
                padding: 6px 14px;
                min-width: 80px;
            }
            QPushButton:hover {
                background-color: #45475a;
                border-color: #89b4fa;
            }
            QPushButton#apply_btn {
                background-color: #89b4fa;
                color: #1e1e2e;
                border: none;
                font-weight: bold;
            }
            QPushButton#apply_btn:hover {
                background-color: #b4befe;
            }
            QPushButton#apply_btn:disabled {
                background-color: #45475a;
                color: #6c7086;
            }
            QFrame#divider {
                background-color: #313244;
                max-height: 1px;
                border: none;
            }
            QScrollArea {
                border: none;
                background: transparent;
            }
            QWidget#fields_panel {
                background: transparent;
            }
            QWidget#fields_inner {
                background: transparent;
            }
            QScrollBar:vertical {
                background: #1e1e2e;
                width: 6px;
            }
            QScrollBar::handle:vertical {
                background: #45475a;
                border-radius: 3px;
            }
        """)

    # ──────────────────────────────────────────────
    # UI construction
    # ──────────────────────────────────────────────

    def _build_ui(self):
        """Construct the static UI components and layout."""
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())
        root.addWidget(self._divider())
        root.addWidget(self._build_card_type_row())
        root.addWidget(self._divider())
        root.addWidget(self._build_content_section())
        root.addWidget(self._divider())
        root.addWidget(self._build_footer())

    def _divider(self) -> QFrame:
        """Return a horizontal divider line widget."""
        line = QFrame()
        line.setObjectName("divider")
        line.setFixedHeight(1)
        return line

    def _build_header(self) -> QWidget:
        """Build the header section with title and deck selector."""
        w = QWidget()
        w.setStyleSheet("background-color: #181825; padding: 2px 0;")
        layout = QHBoxLayout(w)
        layout.setContentsMargins(16, 12, 16, 12)

        title = QLabel("Add to Anki")
        title.setStyleSheet("font-size: 15px; font-weight: bold; color: #cdd6f4;")
        layout.addWidget(title)
        layout.addStretch()

        layout.addWidget(QLabel("Deck:"))
        default_deck = getattr(config, "default_anki_deck", None)
        self._deck_combo = QComboBox()
        deck_items = _with_config_default(default_deck, _FALLBACK_DECKS)
        self._deck_combo.addItems(deck_items)
        if default_deck:
            self._deck_combo.setCurrentText(default_deck)
        layout.addWidget(self._deck_combo)

        refresh_btn = QPushButton("⟳")
        refresh_btn.setToolTip("Refresh decks and card types from Anki")
        refresh_btn.setFixedWidth(32)
        refresh_btn.clicked.connect(self._refresh_from_anki)
        layout.addWidget(refresh_btn)
        return w

    def _build_card_type_row(self) -> QWidget:
        """Build the row containing the card type (model) selector."""
        w = QWidget()
        layout = QHBoxLayout(w)
        layout.setContentsMargins(16, 10, 16, 10)

        lbl = QLabel("Card Type:")
        lbl.setObjectName("section_label")
        layout.addWidget(lbl)

        default_model = getattr(config, "default_anki_model", None)
        self._model_combo = QComboBox()
        model_items = _with_config_default(default_model, _FALLBACK_MODELS)
        self._model_combo.addItems(model_items)
        if default_model:
            self._model_combo.setCurrentText(default_model)
        self._model_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._model_combo.currentTextChanged.connect(self._on_model_changed)
        layout.addWidget(self._model_combo)
        return w

    def _build_content_section(self) -> QWidget:
        """Build the main content section with screenshot panel and field inputs."""
        w = QWidget()
        layout = QHBoxLayout(w)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(16)

        layout.addWidget(self._build_screenshot_panel())
        layout.addWidget(self._build_fields_panel(), 1)
        return w

    def _build_screenshot_panel(self) -> QWidget:
        """Build the panel containing screenshot preview and actions."""
        panel = QWidget()
        panel.setObjectName("screenshot_panel")
        panel.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # Preview
        self._screenshot_preview = QLabel()
        self._screenshot_preview.setFixedSize(160, 100)
        self._screenshot_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._screenshot_preview.setStyleSheet(
            "background-color: #313244; border: 1px solid #45475a; border-radius: 4px; color: #6c7086;"
        )
        self._screenshot_preview.setText("No image")
        layout.addWidget(self._screenshot_preview, alignment=Qt.AlignmentFlag.AlignLeft)

        self._screenshot_action_combo = QComboBox()
        self._screenshot_action_combo.setToolTip("Select screenshot action.")
        self._screenshot_action_combo.addItem("Screenshot Action…")
        self._screenshot_action_combo.addItem("Take Screenshot (Region)")
        self._screenshot_action_combo.addItem("Take Screenshot (Full Screen)")
        self._screenshot_action_combo.addItem("Select File…")
        self._screenshot_action_combo.addItem("Clear")
        self._screenshot_action_combo.currentIndexChanged.connect(self._on_screenshot_action_selected)
        layout.addWidget(self._screenshot_action_combo)

        field_row = QHBoxLayout()
        field_row.setSpacing(6)
        field_row.addWidget(QLabel("Into field:"))
        self._screenshot_field_combo = QComboBox()
        self._screenshot_field_combo.setMinimumWidth(100)
        field_row.addWidget(self._screenshot_field_combo)
        layout.addLayout(field_row)
        layout.addStretch()
        return panel

    def _build_fields_panel(self) -> QWidget:
        """Build the panel containing field input widgets."""
        container = QWidget()
        container.setObjectName("fields_panel")
        outer = QVBoxLayout(container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)

        lbl = QLabel("Fields")
        lbl.setObjectName("section_label")
        outer.addWidget(lbl)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(220)

        self._fields_widget = QWidget()
        self._fields_widget.setObjectName("fields_inner")
        self._fields_form = QFormLayout(self._fields_widget)
        self._fields_form.setContentsMargins(0, 4, 0, 4)
        self._fields_form.setSpacing(8)
        self._fields_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

        scroll.setWidget(self._fields_widget)
        outer.addWidget(scroll)
        return container

    def _build_footer(self) -> QWidget:
        """Build the footer with status label, cancel and apply buttons."""
        w = QWidget()
        w.setStyleSheet("background-color: #181825;")
        layout = QHBoxLayout(w)
        layout.setContentsMargins(16, 12, 16, 12)

        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color: #a6adc8; font-size: 12px;")
        layout.addWidget(self._status_label)
        layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.hide)
        layout.addWidget(cancel_btn)

        self._apply_btn = QPushButton("Accept")
        self._apply_btn.setObjectName("apply_btn")
        self._apply_btn.clicked.connect(self._submit_to_anki)
        layout.addWidget(self._apply_btn)
        return w

    # ──────────────────────────────────────────────
    # Data population
    # ──────────────────────────────────────────────

    def _snapshot_field_values(self):
        """Persist current visible field values into cache before UI rebuilds."""
        for name, inp in self._field_inputs.items():
            self._field_value_cache[name] = inp.toPlainText()

    def _rebuild_fields(self, field_names: List[str]):
        """Rebuild the field input widgets based on the given list of field names, 
        preserving existing values where possible."""
        self._snapshot_field_values()

        previous_screenshot_field = None
        if self._screenshot_field_combo is not None:
            selected = self._screenshot_field_combo.currentText()
            if selected and selected != "(none)":
                previous_screenshot_field = selected

        # Clear existing
        while self._fields_form.rowCount():
            self._fields_form.removeRow(0)
        self._field_inputs.clear()

        if self._screenshot_field_combo is not None:
            self._screenshot_field_combo.clear()
            self._screenshot_field_combo.addItem("(none)")
            self._screenshot_field_combo.addItems(field_names)

        entry = self._current_entry()

        for name in field_names:
            inp = ResizableTextEdit()
            inp.setPlaceholderText(name)
            cached = self._field_value_cache.get(name)
            if cached is not None:
                inp.setText(cached)
            else:
                autofill = self._autofill(name, entry)
                inp.setText(autofill)
                if autofill:
                    self._field_value_cache[name] = autofill
            inp.textChanged.connect(self._update_apply_button_state)
            lbl = QLabel(name + ":")
            lbl.setStyleSheet("color: #a6adc8;")
            self._fields_form.addRow(lbl, inp)
            self._field_inputs[name] = inp

        # Restore screenshot field selection if still available, otherwise auto-select.
        if self._screenshot_field_combo is not None:
            restored = False
            if previous_screenshot_field:
                idx = self._screenshot_field_combo.findText(previous_screenshot_field)
                if idx >= 0:
                    self._screenshot_field_combo.setCurrentIndex(idx)
                    restored = True
            if not restored:
                for i in range(1, self._screenshot_field_combo.count()):
                    if self._screenshot_field_combo.itemText(i).lower() in _SCREEN_KEYS:
                        self._screenshot_field_combo.setCurrentIndex(i)
                        break

        self._fields_widget.adjustSize()
        self._update_apply_button_state()

    def _autofill(self, field_name: str, entry: Optional[DictionaryEntry | KanjiEntry]) -> str:
        """Given a field name and dictionary entry, return an autofill value based on heuristics."""
        key = field_name.lower()
        if key in _WORD_KEYS:
            return entry.written_form if entry and isinstance(entry, DictionaryEntry) else (
                entry.character if entry and isinstance(entry, KanjiEntry) else ""
            )
        if key == "back":
            if entry and isinstance(entry, DictionaryEntry):
                reading = _kata_to_hira(entry.reading or entry.written_form)
                definition = _first_gloss(entry)
                parts = [p for p in [reading, definition] if p]
                return "\n".join(parts)
            if entry and isinstance(entry, KanjiEntry):
                reading = ", ".join(_kata_to_hira(r) for r in entry.readings)
                definition = ", ".join(entry.meanings)
                parts = [p for p in [reading, definition] if p]
                return "\n".join(parts)
            return ""
        if key in _READING_KEYS:
            if entry and isinstance(entry, DictionaryEntry):
                return entry.reading or entry.written_form
            if entry and isinstance(entry, KanjiEntry):
                return ", ".join(entry.readings)
            return ""
        if key in _MEANING_KEYS:
            if entry and isinstance(entry, DictionaryEntry):
                return _first_gloss(entry)
            if entry and isinstance(entry, KanjiEntry):
                return ", ".join(entry.meanings)
            return ""
        return ""

    def _current_entry(self) -> Optional[DictionaryEntry]:
        """Return the current dictionary entry from the popup, if available."""
        data = self.popup_window.get_latest_data()
        return data[0] if data else None

    def _populate_from_lookup(self, force_overwrite: bool = False):
        """Fill fields based on current dictionary entry and field name heuristics. 
        If *force_overwrite* is False, existing field values will not be overwritten."""
        entry = self._current_entry()
        for name, inp in self._field_inputs.items():
            if not force_overwrite and inp.toPlainText().strip():
                continue
            text = self._autofill(name, entry)
            if text:
                inp.setText(text)
                self._field_value_cache[name] = text
        # Try to load last scan screenshot
        if hasattr(self.screen_manager, "last_screenshot") and self.screen_manager.last_screenshot:
            self._load_pil_screenshot(self.screen_manager.last_screenshot)

    def _load_pil_screenshot(self, pil_image):
        """Load a PIL Image as the current screenshot, saving to a temp file and updating preview."""
        try:
            import io
            buf = io.BytesIO()
            pil_image.save(buf, format="PNG")
            buf.seek(0)

            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False, prefix="meikipop_anki_")
            tmp.write(buf.read())
            tmp.close()

            self._cleanup_temp_screenshot()
            self._temp_screenshot = tmp.name
            self._set_screenshot(tmp.name)
        except Exception as e:
            logger.warning(f"Could not load scan screenshot: {e}")

    # ──────────────────────────────────────────────
    # Screenshot handling
    # ──────────────────────────────────────────────

    def _set_screenshot(self, path: str):
        """Set the current screenshot to *path*, update preview, and select target field if applicable."""
        self._screenshot_path = path
        pixmap = QPixmap(path)
        if not pixmap.isNull():
            scaled = pixmap.scaled(
                self._screenshot_preview.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._screenshot_preview.setPixmap(scaled)
            self._screenshot_preview.setText("")
        else:
            self._screenshot_preview.setText("Preview unavailable")

    def _on_screenshot_action_selected(self, index: int):
        """Handle user selecting a screenshot action from the combo box."""
        if index <= 0:
            return
        if index == 1:
            self._take_screenshot(capture_full_screen=False)
        elif index == 2:
            self._take_screenshot(capture_full_screen=True)
        elif index == 3:
            self._select_screenshot_file()
        elif index == 4:
            self._clear_screenshot()

        # Reset back to placeholder so users can run the same action repeatedly.
        if self._screenshot_action_combo is not None:
            self._screenshot_action_combo.blockSignals(True)
            self._screenshot_action_combo.setCurrentIndex(0)
            self._screenshot_action_combo.blockSignals(False)

    def _take_screenshot(self, capture_full_screen: Optional[bool] = None):
        """Hide dialog, then take a region screenshot (Shift = full screen), then restore."""
        if capture_full_screen is None:
            capture_full_screen = bool(
                QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier
            )
        self.hide()

        def do_capture():
            try:
                screen = QApplication.primaryScreen() or QApplication.screens()[0]

                if capture_full_screen:
                    pixmap = screen.grabWindow(int(0))  # type: ignore[arg-type]
                else:
                    region = RegionSelector.get_region()
                    if region is None or region.isNull() or region.width() <= 1 or region.height() <= 1:
                        logger.debug("AnkiSaveDialog: Region capture canceled or too small.")
                        return
                    target_screen = QApplication.screenAt(region.center()) or screen
                    full_pixmap = target_screen.grabWindow(int(0))  # type: ignore[arg-type]
                    screen_geo = target_screen.geometry()
                    local_rect = region.translated(-screen_geo.x(), -screen_geo.y())
                    pixmap = full_pixmap.copy(local_rect)

                if pixmap.isNull():
                    logger.warning("AnkiSaveDialog: Screenshot capture returned an empty image.")
                    return

                tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False, prefix="meikipop_anki_")
                tmp.close()
                pixmap.save(tmp.name, "PNG")

                self._cleanup_temp_screenshot()
                self._temp_screenshot = tmp.name
                self._set_screenshot(tmp.name)
            finally:
                self.show()
                self.raise_()
                self.activateWindow()

        QTimer.singleShot(200, do_capture)

    def _select_screenshot_file(self):
        """Open file dialog to select an image, then set as screenshot if valid."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Image", "",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)"
        )
        if path:
            self._screenshot_path = path
            self._set_screenshot(path)

    def _clear_screenshot(self):
        """Clear the current screenshot from the preview and reset state, 
        including deleting any managed temp file."""
        self._cleanup_temp_screenshot()
        self._screenshot_path = None
        self._screenshot_preview.setPixmap(QPixmap())
        self._screenshot_preview.setText("No image")

    def _cleanup_temp_screenshot(self):
        """If we have a temp screenshot from a region capture, delete the file 
        to avoid littering the user's disk. This should be called whenever we 
        replace or clear the screenshot, and also when the dialog is closed."""
        if self._temp_screenshot and os.path.exists(self._temp_screenshot):
            try:
                os.unlink(self._temp_screenshot)
            except OSError:
                pass
        self._temp_screenshot = None

    # ──────────────────────────────────────────────
    # Anki connection
    # ──────────────────────────────────────────────

    def _refresh_from_anki(self):
        """Attempt to connect to Anki and fetch decks/models. Update combos and status message accordingly."""
        self._set_status("Connecting to Anki…")
        try:
            decks = self._anki.get_deck_names()
            models = self._anki.get_model_names()
        except AnkiConnectError as e:
            self._set_status(f"⚠ {e}", error=True)
            self._update_apply_button_state()
            return

        current_deck = self._deck_combo.currentText()
        self._deck_combo.clear()
        self._deck_combo.addItems(decks)
        if current_deck in decks:
            self._deck_combo.setCurrentText(current_deck)
        elif decks:
            idx = next((i for i, d in enumerate(decks) if d.lower() == "default"), 0)
            self._deck_combo.setCurrentIndex(idx)

        current_model = self._model_combo.currentText()
        self._model_combo.blockSignals(True)
        self._model_combo.clear()
        self._model_combo.addItems(models)
        if current_model in models:
            self._model_combo.setCurrentText(current_model)
        self._model_combo.blockSignals(False)

        self._on_model_changed(self._model_combo.currentText())
        self._set_status("Connected to Anki.", error=False)
        QTimer.singleShot(3000, lambda: self._set_status(""))
        self._update_apply_button_state()

    def _on_model_changed(self, model_name: str):
        """When the user selects a different card type, rebuild the field inputs based on the new model's field names.
        
        Args:
            model_name (str): The name of the selected model.
        """
        try:
            fields = self._anki.get_model_field_names(model_name)
        except AnkiConnectError:
            fields = _FALLBACK_FIELDS.get(model_name, ["Front", "Back"])
        self._rebuild_fields(fields)
        self._update_apply_button_state()

    def _submit_to_anki(self):
        """Gather current field values and send addNote request to AnkiConnect, then show success or error status."""
        deck = self._deck_combo.currentText()
        model = self._model_combo.currentText()
        self._snapshot_field_values()
        fields = {name: inp.toPlainText() for name, inp in self._field_inputs.items()}

        screenshot_field = None
        if self._screenshot_field_combo:
            selected = self._screenshot_field_combo.currentText()
            if selected != "(none)":
                screenshot_field = selected

        self._apply_btn.setEnabled(False)
        self._set_status("Adding note…")
        try:
            note_id = self._anki.add_note(
                deck_name=deck,
                model_name=model,
                fields=fields,
                screenshot_path=self._screenshot_path if screenshot_field else None,
                screenshot_field=screenshot_field,
            )
            logger.info(f"Added Anki note id={note_id} to deck '{deck}'.")
            self._set_status(f"✓ Note added (id {note_id})")
            QTimer.singleShot(1500, self.hide)
        except AnkiConnectError as e:
            self._set_status(f"⚠ {e}", error=True)
        finally:
            self._apply_btn.setEnabled(True)
            self._update_apply_button_state()

    # ──────────────────────────────────────────────
    # Hotkey listener
    # ──────────────────────────────────────────────

    def _start_hotkey_listener(self):
        """Start a global hotkey listener in a separate thread that emits _show_signal when triggered."""
        hotkey_str = getattr(config, "anki_save_hotkey", "ctrl+shift+s")
        pynput_hotkey = _hotkey_to_pynput(hotkey_str)
        try:
            from pynput import keyboard
            self._hotkey_listener = keyboard.GlobalHotKeys(
                {pynput_hotkey: lambda: self._show_signal.emit()}
            )
            self._hotkey_listener.start()
            logger.debug(f"AnkiSaveDialog: Hotkey listener started for '{pynput_hotkey}'.")
        except Exception as e:
            logger.warning(f"AnkiSaveDialog: Could not start hotkey listener: {e}")

    def _on_hotkey_triggered(self):
        """Called on the main thread via signal."""
        if not config.enable_anki_integration:
            return
        if self.isVisible():
            self.hide()
            return
        self._refresh_from_anki()
        # Refresh with the latest lookup each time the dialog is opened.
        self._populate_from_lookup(force_overwrite=True)
        self._update_apply_button_state()
        self.show()
        self.raise_()
        self.activateWindow()

    # ──────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────

    def _set_status(self, msg: str, error: bool = False):
        """Set the status message in the footer, optionally styling it as an error."""
        self._status_label.setText(msg)
        color = "#f38ba8" if error else "#a6adc8"
        self._status_label.setStyleSheet(f"color: {color}; font-size: 12px;")

    def _update_apply_button_state(self):
        """Enable the Apply button only if required fields are filled and Anki is reachable."""
        if not hasattr(self, "_apply_btn"):
            return
        has_deck = bool(self._deck_combo.currentText().strip()) if hasattr(self, "_deck_combo") else False
        has_model = bool(self._model_combo.currentText().strip()) if hasattr(self, "_model_combo") else False
        has_fields = len(self._field_inputs) > 0

        # Anki rejects notes when the first field is empty for most note types.
        first_field_has_text = False
        if has_fields:
            first_input = next(iter(self._field_inputs.values()))
            first_field_has_text = bool(first_input.toPlainText().strip())

        self._apply_btn.setEnabled(has_deck and has_model and has_fields and first_field_has_text)

    def closeEvent(self, a0):
        """Override closeEvent to hide instead of close, so the dialog can be reused without re-instantiation."""
        if a0:
            a0.ignore()
        self.hide()

    def hideEvent(self, a0):
        super().hideEvent(a0)

    def __del__(self):
        self._cleanup_temp_screenshot()
        if self._hotkey_listener:
            try:
                self._hotkey_listener.stop()
            except Exception:
                pass


class ResizableTextEdit(QTextEdit):
    """A QTextEdit with a small bottom-right drag handle for resizing height."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._resizing = False
        self._resize_start_pos = QPoint()
        self._resize_start_height = 0
        self._grip_size = 14
        self.setAcceptRichText(False)
        self.setMinimumHeight(36)
        self.setMaximumHeight(180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

    def _grip_rect(self):
        return self.rect().adjusted(
            self.width() - self._grip_size,
            self.height() - self._grip_size,
            -1,
            -1,
        )

    def mousePressEvent(self, e: Optional[QMouseEvent]):
        if e is None:
            return
        if e.button() == Qt.MouseButton.LeftButton and self._grip_rect().contains(e.position().toPoint()):
            self._resizing = True
            self._resize_start_pos = e.globalPosition().toPoint()
            self._resize_start_height = self.height()
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e: Optional[QMouseEvent]):
        if e is None:
            return
        if self._resizing:
            delta = e.globalPosition().toPoint().y() - self._resize_start_pos.y()
            new_height = max(self.minimumHeight(), min(self.maximumHeight(), self._resize_start_height + delta))
            self.setFixedHeight(new_height)
            e.accept()
            return

        if self._grip_rect().contains(e.position().toPoint()):
            self.setCursor(QCursor(Qt.CursorShape.SizeFDiagCursor))
        else:
            self.unsetCursor()
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e: Optional[QMouseEvent]):
        if e is None:
            return
        if self._resizing and e.button() == Qt.MouseButton.LeftButton:
            self._resizing = False
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def leaveEvent(self, a0):
        if not self._resizing:
            self.unsetCursor()
        super().leaveEvent(a0)

    def paintEvent(self, e):
        super().paintEvent(e)
        viewport = self.viewport()
        if viewport is None:
            return
        painter = QPainter(viewport)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        pen = QPen(Qt.GlobalColor.lightGray)
        painter.setPen(pen)

        x2 = viewport.width() - 4
        y2 = viewport.height() - 4
        painter.drawLine(x2 - 6, y2, x2, y2 - 6)
        painter.drawLine(x2 - 10, y2, x2, y2 - 10)
