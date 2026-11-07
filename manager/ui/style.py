"""Shared manager skin: raised-bevel panel look, one palette for every tab.

QSS can't do shadows, so the bevel is per-side border colors - light source from
the top: top/left edges catch light, right/bottom fall into shadow (raised)."""

PANEL_TOP = "#3a4550"
PANEL_LEFT = "#2c3641"
PANEL_RIGHT = "#0d1218"
PANEL_BOTTOM = "#07090d"
PANEL_BG_TOP = "#1c242d"
PANEL_BG_BOTTOM = "#151b23"

_BEVEL = (
    f"border: 1px solid;"
    f" border-top-color: {PANEL_TOP}; border-left-color: {PANEL_LEFT};"
    f" border-right-color: {PANEL_RIGHT}; border-bottom-color: {PANEL_BOTTOM};"
    f" background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
    f" stop:0: {PANEL_BG_TOP}, stop:1: {PANEL_BG_BOTTOM});"
)

# The one accent: section headers, group-box titles, small highlights.
ACCENT = "#4FC3F7"

# Text tokens: one dim gray, one bright text, the status triad, the mono face.
DIM = "#8a939c"
TEXT = "#dfe6ec"
GOOD = "#4ADE80"
WARN = "#FBBF24"
ERROR = "#F87171"
MONO = "font-family: Consolas;"

# Muted italic hint labels; status_qss() covers their colored siblings.
HINT_QSS = f"color: {DIM}; font-style: italic;"


def status_qss(color, italic=True):
    """Colored status-label style (setup/clone hints, warnings)."""
    qss = f"color: {color};"
    return qss + " font-style: italic;" if italic else qss

# Group boxes everywhere else (User Profile, System Environment, preset dialog).
# Title styled like the audio tab's section labels; no font-weight on the box
# itself (QSS fonts cascade to every child widget).
GROUPBOX_QSS = f"""
QGroupBox {{ {_BEVEL}
    border-radius: 6px; margin-top: 10px; color: #E6E6E6; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px;
    color: {ACCENT}; font-size: 10px; letter-spacing: 2px; }}
"""

# Shared console look for read-only log views (pipeline, setup, voice test).
CONSOLE_COLOR = "#9CDCFE"
CONSOLE_QSS = f"background-color: #121212; color: {CONSOLE_COLOR}; font-family: Consolas;"

# Slim app-wide scrollbars: transparent track (adapts to any surface), rounded
# handle, no arrow decorations.
SCROLLBAR_QSS = """
QScrollBar:vertical { width: 6px; background: transparent; border: none; }
QScrollBar::handle:vertical { background: #2e3a44; border-radius: 3px; min-height: 20px; }
QScrollBar::handle:vertical:hover { background: #3a4550; }
QScrollBar:horizontal { height: 6px; background: transparent; border: none; }
QScrollBar::handle:horizontal { background: #2e3a44; border-radius: 3px; min-width: 20px; }
QScrollBar::handle:horizontal:hover { background: #3a4550; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }
"""

# The single application stylesheet (manager/main.py).
APP_QSS = GROUPBOX_QSS + SCROLLBAR_QSS

# Layout rhythm: one geometry system for every tab's bezels.
PAGE_MARGIN = 8
BEZEL_PAD_X = 10
BEZEL_PAD_TOP = 8
BEZEL_PAD_BOTTOM = 6
BEZEL_GAP = 4


def make_group(title, horizontal=False):
    """The ONE way to build a bezel section anywhere in the manager: shared title
    style (app-wide QSS) and shared padding rhythm. Returns (group box, layout)."""
    from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QVBoxLayout

    box = QGroupBox(title)
    layout = QHBoxLayout(box) if horizontal else QVBoxLayout(box)
    layout.setContentsMargins(BEZEL_PAD_X, BEZEL_PAD_TOP, BEZEL_PAD_X, BEZEL_PAD_BOTTOM)
    layout.setSpacing(6)
    return box, layout
