# meikipop/gui/popup_furigana.py
import logging
from typing import Tuple

from PyQt6.QtCore import QPoint, QSize, QTimer
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QCursor, QFont
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel, QFrame, QApplication, QGraphicsDropShadowEffect

from meikipop.config.config import config, IS_MACOS
from meikipop.dictionary.lookup import KanjiEntry, DictionaryEntry
from meikipop.gui.magpie_manager import magpie_manager

# macOS-specific imports for focus management
if IS_MACOS:
    try:
        import Quartz
    except ImportError:
        Quartz = None

logger = logging.getLogger(__name__)


class FuriganaPopup(QWidget):
    def __init__(self, input_loop, parent=None):
        super().__init__(parent)
        self._latest_data = None
        self._displayed_entry = None
        self._previous_active_window_on_mac = None
        self.is_visible = False
        self.input_loop = input_loop

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet("background: transparent;")

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.process_latest_data_loop)
        self.timer.start(10)

        self.frame = QFrame()
        main_layout.addWidget(self.frame)

        self.content_layout = QVBoxLayout(self.frame)
        self.content_layout.setContentsMargins(10, 8, 10, 8)

        self.display_label = QLabel()
        self.display_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.display_label.setTextFormat(Qt.TextFormat.RichText)
        self.shadow_padding = 4
        text_shadow = QGraphicsDropShadowEffect(self.display_label)
        text_shadow.setBlurRadius(6)
        text_shadow.setOffset(1, 1)
        text_shadow.setColor(QColor(0, 0, 0, 180))
        self.display_label.setGraphicsEffect(text_shadow)
        self.content_layout.addWidget(self.display_label)

        self.probe_label = QLabel()
        self.probe_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.probe_label.setTextFormat(Qt.TextFormat.RichText)

        self._apply_frame_stylesheet()
        self.hide()

    def _apply_frame_stylesheet(self):
        font = QFont(config.font_family)
        font.setPixelSize(config.font_size_header)
        self.display_label.setFont(font)
        self.probe_label.setFont(font)
        self.frame.setStyleSheet(f"""
            QFrame {{
                background-color: transparent;
                border: none;
            }}
            QLabel {{
                background-color: transparent;
                border: none;
                color: {config.color_foreground};
                font-family: \"{config.font_family}\";
            }}
        """)
    
    def process_latest_data_loop(self):
        hotkey_down = self.input_loop.is_virtual_hotkey_down()

        mouse_pos = QCursor.pos()
        if self._latest_data and hotkey_down and config.is_enabled:
            if self._latest_data is not self._displayed_entry:
                self._apply_entry_to_label(self._latest_data)
            if not self.is_visible:
                self.show_popup()
            self.move_to(mouse_pos.x(), mouse_pos.y())
        else:
            self.hide_popup()
        


    def _extract_surface_and_reading(self, entry: DictionaryEntry | KanjiEntry) -> Tuple[str, str]:
        # Lookup passes DictionaryEntry as the first result. Support that shape directly.
        if isinstance(entry, DictionaryEntry):
            return entry.written_form or "", entry.reading or ""

        surface = (
            getattr(entry, "kanji", None)
            or getattr(entry, "character", None)
            or getattr(entry, "written_form", "")
        )
        reading = getattr(entry, "furigana", None) or getattr(entry, "reading", "")
        return surface, reading

    def _build_furigana_html(self, surface: str, reading: str) -> tuple[str, str]:
        plain_text = f"{surface} [{reading}]"
        html = (
            f'<span style="color: {config.color_highlight_word}; '
            f'font-size:{config.font_size_header}px;">{surface}</span>'
            f' <span style="color: {config.color_highlight_reading}; '
            f'font-size:{max(config.font_size_header - 2, 1)}px;">[{reading}]</span>'
        )
        return plain_text, html

    def _calculate_size(self, text_html: str) -> QSize:
        self.probe_label.setText(text_html)
        content_size = self.probe_label.sizeHint()

        margins = self.content_layout.contentsMargins()
        border_width = 0
        horizontal_padding = margins.left() + margins.right() + (border_width * 2)
        vertical_padding = margins.top() + margins.bottom() + (border_width * 2)

        return QSize(
            content_size.width() + horizontal_padding + (self.shadow_padding * 2),
            content_size.height() + vertical_padding + (self.shadow_padding * 2),
        )

    def set_furigana_data(self, entry: DictionaryEntry | KanjiEntry):
        # Called from the Lookup background thread — only store data, no Qt widget calls.
        self._latest_data = entry

    def _apply_entry_to_label(self, entry: DictionaryEntry | KanjiEntry):
        """Update display label for *entry*. Must be called from the main thread."""
        surface, reading = self._extract_surface_and_reading(entry)
        if not surface or not reading:
            self._latest_data = None
            return
        _, text_html = self._build_furigana_html(surface, reading)
        self.display_label.setText(text_html)
        self.setFixedSize(self._calculate_size(text_html))
        self._displayed_entry = entry

    def show_furigana(self, entry: DictionaryEntry | KanjiEntry):
        if not entry:
            self.hide_popup()
            return

        surface, reading = self._extract_surface_and_reading(entry)
        if not surface or not reading:
            self.hide_popup()
            return

        _, text_html = self._build_furigana_html(surface, reading)
        self.display_label.setText(text_html)
        self.setFixedSize(self._calculate_size(text_html))

        cursor_pos = QCursor.pos()
        self.show_popup()
        self.move_to(cursor_pos.x(), cursor_pos.y())

    def move_to(self, x, y):
        cursor_point = QPoint(x, y)
        screen = QApplication.screenAt(cursor_point) or QApplication.primaryScreen()
        screen_geo = screen.geometry()
        popup_size = self.size()
        offset = 12

        ratio = screen.devicePixelRatio()
        x, y = magpie_manager.transform_raw_to_visual((int(x), int(y)), ratio)

        final_x = x - (popup_size.width() / 2.0)
        final_y = y - popup_size.height() - offset

        if final_y < screen_geo.top():
            final_y = y + offset

        final_x = max(screen_geo.left(), min(final_x, screen_geo.right() - popup_size.width()))
        final_y = max(screen_geo.top(), min(final_y, screen_geo.bottom() - popup_size.height()))

        self.move(int(final_x), int(final_y))

    def show_popup(self):
        if self.is_visible:
            return

        self._store_active_window_on_mac()
        self.show()
        if IS_MACOS:
            self.raise_()

        self.is_visible = True

    def hide_popup(self):
        if not self.is_visible:
            return

        self.hide()
        self.is_visible = False
        self._restore_focus_on_mac()

    def reapply_settings(self):
        logger.debug("FuriganaPopup: Re-applying settings.")
        self._apply_frame_stylesheet()

    def _store_active_window_on_mac(self):
        """Store the currently active window for focus restoration (macOS only)."""
        if not IS_MACOS or not Quartz:
            return

        try:
            active_app = Quartz.NSWorkspace.sharedWorkspace().frontmostApplication()
            if active_app:
                self._previous_active_window_on_mac = active_app
        except Exception as e:
            logger.warning(f"Failed to store active window: {e}")
            self._previous_active_window_on_mac = None

    def _restore_focus_on_mac(self):
        """Restore focus to the previously active application (macOS only)."""
        if not IS_MACOS or not Quartz or not self._previous_active_window_on_mac:
            return

        try:
            self._previous_active_window_on_mac.activateWithOptions_(Quartz.NSApplicationActivateAllWindows)
        except Exception as e:
            logger.warning(f"Failed to restore focus: {e}")
        finally:
            self._previous_active_window_on_mac = None