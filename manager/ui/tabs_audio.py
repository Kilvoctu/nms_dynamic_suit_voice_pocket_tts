# manager/ui/tabs_audio.py - DAW-style eqfx editor: preset bar, channel strip, EQ analyzer, transport.
import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPainterPath, QPen, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ui.style import (
    ACCENT as _ACCENT,
    BEZEL_GAP,
    BEZEL_PAD_BOTTOM,
    BEZEL_PAD_TOP,
    BEZEL_PAD_X,
    CONSOLE_COLOR,
    DIM as _DIM,
    MONO,
    PAGE_MARGIN,
    TEXT,
    make_group,
)
from ui.workers import FunctionThread
from engine import tts_utils
from engine.fx import MAX_EQ_BANDS as _MAX_EQ_BANDS

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

_POCKET_TTS_TERMS_URL = "https://huggingface.co/kyutai/pocket-tts"

_BASE_CATALOG = {
    "marius": ("Marius", "MALE"),
    "javert": ("Javert", "MALE"),
    "jean": ("Jean", "MALE"),
    "alba": ("Alba", "MALE"),
    "fantine": ("Fantine", "FEMALE"),
    "cosette": ("Cosette", "FEMALE"),
    "eponine": ("Eponine", "FEMALE"),
    "azelma": ("Azelma", "FEMALE"),
}


def _pocket_model(window) -> str:
    """Configured Pocket TTS model key, falling back to the code default."""
    try:
        user_data = json.loads(window.user_config_file.read_text(encoding="utf-8"))
        return user_data.get("models", {}).get("pocket_model") or tts_utils.POCKET_DEFAULT_MODEL
    except Exception:
        return tts_utils.POCKET_DEFAULT_MODEL

_NO_PRESET = "(no preset - custom rack)"
_FACTORY_PRESET = "Factory Standard Exosuit Core"
_EQ_TYPES = ("lowshelf", "peaking", "highshelf", "highpass", "lowpass", "notch")
_CANVAS_SR = 40000  # the render chain's FX canvas (synth is upsampled to it before EQ/FX)
_AXIS_MAX_HZ = 20000.0  # 40 kHz FX canvas Nyquist: bands above can't survive the render
TESTER_MIX_ATTENUATION_DB = -10.0  # sandbox playback only: emulates the game's suit-voice bus

# FX unit specs: (slot, title, primary param, [(key, label, min, max, dec, step, default), ...]).
# Values and defaults mirror engine.fx's sanitize_fx clamps.
_FX_CARDS = [
    ("doubler", "Doubler", "level", [
        ("detune_cents", "Detune (cents)", 0.0, 100.0, 0, 1.0, 25.0),
        ("delay_up_ms", "Delay up (ms)", 0.0, 60.0, 0, 1.0, 11.0),
        ("delay_down_ms", "Delay down (ms)", 0.0, 60.0, 0, 1.0, 18.0),
        ("level", "Level", 0.0, 1.0, 2, 0.05, 0.28),
    ]),
    ("crusher", "Bit Crusher", "bits", [
        ("bits", "Bit depth", 2.0, 16.0, 0, 1.0, 12.0),
        ("mix", "Mix", 0.0, 1.0, 2, 0.05, 0.05),
    ]),
    ("decimate", "Decimator", "rate_hz", [
        ("rate_hz", "Rate (Hz)", 1000.0, 22000.0, 0, 100.0, 8000.0),
        ("mix", "Mix", 0.0, 1.0, 2, 0.05, 0.25),
    ]),
    ("ringmod", "Ring Mod", "mix", [
        ("freq_hz", "Carrier (Hz)", 20.0, 1000.0, 0, 5.0, 300.0),
        ("mix", "Mix", 0.0, 1.0, 2, 0.05, 1.0),
    ]),
    ("radio", "Radio Band", "mix", [
        ("hp_hz", "High-pass (Hz)", 20.0, 20000.0, 0, 10.0, 750.0),
        ("lp_hz", "Low-pass (Hz)", 20.0, 20000.0, 0, 10.0, 7000.0),
        ("bits", "Bit depth", 2.0, 16.0, 0, 1.0, 8.0),
        ("level", "Level", 0.0, 1.0, 2, 0.05, 0.22),
        ("mix", "Mix", 0.0, 1.0, 2, 0.05, 0.6),
    ]),
    ("echo", "Echo", "feedback", [
        ("feedback", "Feedback", 0.0, 0.85, 2, 0.05, 0.0),
        ("damping_hz", "Damping (Hz)", 0.0, 16000.0, 0, 100.0, 0.0),
    ]),
    ("reverb", "Reverb", "wet", [
        ("size", "Size", 0.0, 1.0, 2, 0.05, 0.6),
        ("damp", "Damping", 0.0, 1.0, 2, 0.05, 0.5),
        ("wet", "Wet", 0.0, 1.0, 2, 0.05, 0.35),
    ]),
    ("compressor", "Compressor", "ratio", [
        ("threshold_db", "Threshold (dB)", -60.0, 0.0, 0, 1.0, -20.0),
        ("ratio", "Ratio", 1.0, 20.0, 1, 0.5, 3.0),
        ("attack_ms", "Attack (ms)", 0.1, 100.0, 1, 1.0, 10.0),
        ("release_ms", "Release (ms)", 10.0, 1000.0, 0, 10.0, 120.0),
        ("makeup_db", "Makeup (dB)", 0.0, 12.0, 1, 0.5, 0.0),
    ]),
    ("limiter", "Limiter", "ceiling", [
        ("ceiling", "Ceiling", 0.5, 0.999, 2, 0.01, 0.95),
        ("lookahead_ms", "Lookahead (ms)", 0.0, 50.0, 1, 1.0, 5.0),
        ("release_ms", "Release (ms)", 10.0, 1000.0, 0, 10.0, 80.0),
    ]),
]


def _safe_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip() or "preset"


class _ClickableLabel(QLabel):
    """Readout label that opens an inline editor on double-click."""
    double_clicked = Signal()

    def mouseDoubleClickEvent(self, event):
        self.double_clicked.emit()


class _ValueEditor(QLineEdit):
    """Inline value editor: Enter or focus-out commits, Escape cancels."""
    committed = Signal(str)
    cancelled = Signal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.committed.emit(self.text())
        elif event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
        else:
            super().keyPressEvent(event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.committed.emit(self.text())


def _band_response_db(band, freqs, sr):
    """RBJ biquad magnitude response in dB for one EQ band, vectorized over freqs."""
    f0 = float(np.clip(band.get("freq", 1000.0), 20.0, sr / 2.0 - 1.0))
    q = float(np.clip(band.get("q", 0.707), 0.1, 10.0))
    gain = float(band.get("gain_db", 0.0))
    btype = band.get("type", "peaking")
    w0 = 2 * np.pi * f0 / sr
    cw, sw = np.cos(w0), np.sin(w0)
    alpha = sw / (2.0 * q)
    A = 10 ** (gain / 40.0)
    if btype == "peaking":
        b = [1 + alpha * A, -2 * cw, 1 - alpha * A]
        a = [1 + alpha / A, -2 * cw, 1 - alpha / A]
    elif btype in ("highpass", "lowpass"):
        s = 1 + cw
        b = [s / 2, -s, s / 2] if btype == "highpass" else [(1 - cw) / 2, 1 - cw, (1 - cw) / 2]
        a = [1 + alpha, -2 * cw, 1 - alpha]
    elif btype == "notch":
        b = [1.0, -2 * cw, 1.0]
        a = [1 + alpha, -2 * cw, 1 - alpha]
    else:  # shelves at slope S=1
        alpha = sw / 2.0 * np.sqrt(2.0)
        sa = 2 * np.sqrt(A) * alpha
        if btype == "lowshelf":
            b = [A * ((A + 1) - (A - 1) * cw + sa), 2 * A * ((A - 1) - (A + 1) * cw),
                 A * ((A + 1) - (A - 1) * cw - sa)]
            a = [(A + 1) + (A - 1) * cw + sa, -2 * ((A - 1) + (A + 1) * cw),
                 (A + 1) + (A - 1) * cw - sa]
        else:
            b = [A * ((A + 1) + (A - 1) * cw + sa), -2 * A * ((A - 1) + (A + 1) * cw),
                 A * ((A + 1) + (A - 1) * cw - sa)]
            a = [(A + 1) - (A - 1) * cw + sa, 2 * ((A - 1) - (A + 1) * cw),
                 (A + 1) - (A - 1) * cw - sa]
    z1 = np.exp(-1j * 2 * np.pi * np.asarray(freqs, dtype=float) / sr)
    h = (b[0] + b[1] * z1 + b[2] * z1**2) / (a[0] + a[1] * z1 + a[2] * z1**2)
    mag = np.abs(h)
    return 20 * np.log10(np.maximum(mag, 1e-9))


class EQCurveCanvas(QWidget):
    """Node EQ surface: click empty space to add a band, drag nodes (freq/gain), wheel = Q,
    right-click for type/remove, drag a node past the top/bottom edge to delete it."""

    _DELETE_OVERSHOOT = 18.0

    def __init__(self, parent_tab):
        super().__init__()
        self._tab = parent_tab
        self._bands = []  # live reference to the tab's band list
        self._sel = -1
        self._drag = -1
        self._press_pos = None
        self._delete_pending = False
        self._hover_pos = None
        self._q_feedback = None  # (index, "Q 0.71") transient wheel readout
        self.setMinimumHeight(190)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def attach(self, bands):
        self._bands = bands

    def set_selection(self, index):
        self._sel = index
        self.update()

    # geometry ---------------------------------------------------------
    def _pad(self):
        return 26.0

    def _x_for_freq(self, f, w=None):
        w = (w or self.width()) - 2 * self._pad()
        decades = np.log10(_AXIS_MAX_HZ / 20.0)
        t = max(0.0, min(1.0, np.log10(max(f, 20.0) / 20.0) / decades))  # 20 Hz .. Nyquist
        return self._pad() + t * w

    def _freq_for_x(self, x, w=None):
        w = (w or self.width()) - 2 * self._pad()
        t = (x - self._pad()) / max(w, 1.0)
        return 20.0 * 10 ** (np.log10(_AXIS_MAX_HZ / 20.0) * max(0.0, min(1.0, t)))

    def _y_for_gain(self, g, h=None):
        h = (h or self.height()) - 2 * self._pad()
        return self._pad() + (1.0 - (max(-18.0, min(18.0, g)) + 18.0) / 36.0) * h

    def _gain_for_y(self, y, h=None):
        h = (h or self.height()) - 2 * self._pad()
        return round(((1.0 - (y - self._pad()) / max(h, 1.0)) * 36.0 - 18.0) * 2.0) / 2.0

    # interaction ------------------------------------------------------
    def _node_at(self, pos):
        best, dist = -1, 16.0
        for i, band in enumerate(self._bands):
            dx = pos.x() - self._x_for_freq(band.get("freq", 1000.0))
            dy = pos.y() - self._y_for_gain(band.get("gain_db", 0.0))
            d = (dx * dx + dy * dy) ** 0.5
            if d < dist:
                best, dist = i, d
        return best

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return  # right-button goes through contextMenuEvent
        pos = event.position()
        idx = self._node_at(pos)
        self._press_pos = (pos.x(), pos.y())
        if idx >= 0:
            self._sel = idx
            self._drag = idx
            self._delete_pending = False
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self._tab.on_canvas_selected(idx)
        self.update()

    def mouseMoveEvent(self, event):
        pos = event.position()
        if self._drag < 0 or self._drag >= len(self._bands):
            self._hover_pos = pos
            over = self._node_at(pos) >= 0
            self.setCursor(Qt.CursorShape.PointingHandCursor if over else Qt.CursorShape.CrossCursor)
            self.update()
            return
        band = self._bands[self._drag]
        band["freq"] = int(round(max(20.0, min(_AXIS_MAX_HZ, self._freq_for_x(pos.x())))))
        band["gain_db"] = self._gain_for_y(pos.y())
        pad = self._pad()
        self._delete_pending = (pos.y() < pad - self._DELETE_OVERSHOOT
                                or pos.y() > self.height() - pad + self._DELETE_OVERSHOOT)
        self._tab.on_canvas_band_edited(self._drag)
        self.update()

    def mouseReleaseEvent(self, event):
        pos = event.position()
        if self._drag >= 0:
            if self._delete_pending:
                idx, self._drag, self._delete_pending = self._drag, -1, False
                self.setCursor(Qt.CursorShape.CrossCursor)
                self._tab.on_canvas_delete(idx)
                return
            self._drag = -1
            over = self._node_at(pos) >= 0
            self.setCursor(Qt.CursorShape.PointingHandCursor if over else Qt.CursorShape.CrossCursor)
        elif self._press_pos is not None:
            moved = ((pos.x() - self._press_pos[0]) ** 2 + (pos.y() - self._press_pos[1]) ** 2) ** 0.5
            if moved < 4.0:
                self._tab.on_canvas_add(self._freq_for_x(pos.x()), self._gain_for_y(pos.y()))
        self._press_pos = None
        self.update()

    def leaveEvent(self, _event):
        self._hover_pos = None
        self.update()

    def wheelEvent(self, event):
        idx = self._node_at(event.position())
        if idx < 0:
            return
        band = self._bands[idx]
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        band["q"] = round(max(0.1, min(10.0, band.get("q", 0.707) * factor)), 2)
        self._sel = idx
        self._q_feedback = (idx, f"Q {band['q']:.2f}")
        QTimer.singleShot(900, self._clear_q_feedback)
        self._tab.on_canvas_band_edited(idx)
        self.update()

    def _clear_q_feedback(self):
        self._q_feedback = None
        self.update()

    def contextMenuEvent(self, event):
        pos = event.pos()
        idx = self._node_at(pos)
        menu = QMenu(self)
        if idx >= 0:
            band = self._bands[idx]
            type_menu = menu.addMenu("Type")
            for t in _EQ_TYPES:
                act = type_menu.addAction(t)
                act.setCheckable(True)
                act.setChecked(band.get("type") == t)
                act.triggered.connect(lambda _c, i=idx, ty=t: self._tab.on_canvas_type_changed(i, ty))
            reset = menu.addAction("Reset Q")
            reset.triggered.connect(lambda _c, i=idx: self._tab.on_canvas_reset_q(i))
            menu.addSeparator()
            remove = menu.addAction("Remove band")
            remove.triggered.connect(lambda _c, i=idx: self._tab.on_canvas_delete(i))
        else:
            add = menu.addAction("Add band here")
            add.triggered.connect(
                lambda _c: self._tab.on_canvas_add(self._freq_for_x(pos.x()), self._gain_for_y(pos.y())))
        menu.exec(self.mapToGlobal(pos))

    # painting ---------------------------------------------------------
    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        pad = self._pad()
        p.fillRect(0, 0, w, h, QColor("#0d1115"))

        # grid: log-frequency decades + 6 dB gain lines
        p.setFont(self._mono_font(9))
        for f in (20, 30, 50, 100, 200, 300, 500, 1000, 2000, 3000, 5000, 10000):
            x = self._x_for_freq(f, w)
            p.setPen(QPen(QColor("#1d242c"), 1))
            p.drawLine(int(x), int(pad), int(x), int(h - pad))
            if f in (100, 1000, 10000):
                p.setPen(QPen(QColor("#55606a"), 1))
                label = f"{f // 1000}k" if f >= 1000 else str(f)
                p.drawText(int(x) - 8, h - 8, label)
        for g in range(-18, 19, 6):
            y = self._y_for_gain(g, h)
            p.setPen(QPen(QColor("#2e3a44") if g == 0 else QColor("#1d242c"), 1))
            p.drawLine(int(pad), int(y), int(w - pad), int(y))
            if g in (12, 0, -12):
                p.setPen(QPen(QColor("#55606a"), 1))
                p.drawText(2, int(y) + 4, "0" if g == 0 else f"{g:+d}")

        # ghost node under the cursor over empty space
        if self._hover_pos is not None and self._drag < 0 and self._node_at(self._hover_pos) < 0:
            gx, gy = self._hover_pos.x(), self._hover_pos.y()
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(79, 195, 247, 90), 1))
            p.drawEllipse(int(gx) - 5, int(gy) - 5, 10, 10)

        if not self._bands:
            p.setFont(self._mono_font(10))
            p.setPen(QPen(QColor("#3d4750"), 1))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "click anywhere to add a band")
            return

        fmax = _AXIS_MAX_HZ
        freqs = np.geomspace(20.0, fmax, 240)
        total = np.zeros_like(freqs)
        for band in self._bands:
            total += _band_response_db(band, freqs, _CANVAS_SR)
        total = np.clip(total, -18.0, 18.0)

        path = QPainterPath()
        for i, (f, db) in enumerate(zip(freqs, total)):
            x, y = self._x_for_freq(float(f), w), self._y_for_gain(float(db), h)
            path.lineTo(x, y) if i else path.moveTo(x, y)

        zero_y = self._y_for_gain(0.0, h)
        fill = QPainterPath(path)
        fill.lineTo(self._x_for_freq(fmax, w), zero_y)
        fill.lineTo(self._x_for_freq(20.0, w), zero_y)
        grad = QLinearGradient(0, pad, 0, h - pad)
        grad.setColorAt(0.0, QColor(79, 195, 247, 70))
        grad.setColorAt(1.0, QColor(79, 195, 247, 0))
        p.fillPath(fill, QBrush(grad))

        p.setPen(QPen(QColor(_ACCENT), 2))
        p.drawPath(path)

        # nodes: accent when idle, amber ring for the selected band, red x when delete-pending
        for i, band in enumerate(self._bands):
            x = self._x_for_freq(band.get("freq", 1000.0), w)
            y = self._y_for_gain(band.get("gain_db", 0.0), h)
            pending = self._delete_pending and i == self._drag
            color = QColor(79, 195, 247, 110) if pending else QColor(_ACCENT)
            p.setBrush(QBrush(color))
            p.setPen(QPen(color, 1))
            p.drawEllipse(int(x) - 4, int(y) - 4, 8, 8)
            if pending:
                p.setPen(QPen(QColor("#ff6b6b"), 2))
                p.drawText(int(x) - 4, int(y) - 10, "\u2715")
            elif i == self._sel:
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor("#FFD166"), 2))
                p.drawEllipse(int(x) - 7, int(y) - 7, 14, 14)
            if self._q_feedback and self._q_feedback[0] == i:
                p.setPen(QPen(QColor("#FFD166"), 1))
                p.drawText(int(x) + 10, int(y) - 8, self._q_feedback[1])

    @staticmethod
    def _mono_font(size: int):
        from PySide6.QtGui import QFont

        f = QFont("Consolas")
        f.setPixelSize(size)
        return f


class AudioTab(QWidget):
    """DAW-style rack editor: preset bar, channel strip (delivery + FX), EQ analyzer, transport."""

    def __init__(self, window):
        super().__init__()
        self.setObjectName("AudioTab")
        self.setStyleSheet(
            "#AudioTab { background: #14181d; } "
            "#AudioTab QLabel { background: transparent; }"
        )
        self.window = window
        self.builtin_file = window.project_root / "assets" / "data" / "eqfx_presets.json"
        self.user_eqfx_dir = window.project_root / "state" / "user" / "eqfx"
        self.active_file = window.project_root / "state" / "user" / "active_eqfx.json"
        self.builtin_presets = self._read_json_map(self.builtin_file, "eqfx_presets.json")
        self._migrate_legacy_user_presets()
        self.user_presets = self._load_user_presets()
        self._bands = []
        self._sel_band = -1
        self._syncing = False
        self._echo_taps = {}
        self._radio_bands = []
        self._ab_stash = None
        self._test_thread = None
        self._initializing = True
        self._build_ui()
        self._rebuild_voice_combo()
        self._load_initial_state()
        self._initializing = False

    # ------------------------------------------------------------ data
    def _read_json_map(self, path, label):
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            return data if isinstance(data, dict) else {}
        except Exception as e:
            print(f"Preset Loader: Failed to parse {label}: {e}")
            return {}

    def _load_user_presets(self):
        """User presets are one JSON file each in state/user/eqfx/ (eqfx rack schema)."""
        presets = {}
        if self.user_eqfx_dir.exists():
            for f in sorted(self.user_eqfx_dir.glob("*.json")):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except Exception as e:
                    print(f"Preset Loader: Failed to parse {f.name}: {e}")
                    continue
                if isinstance(data, dict):
                    name = str(data.get("name") or f.stem)
                    data["name"] = name
                    presets[name] = data
        return presets

    def _migrate_legacy_user_presets(self):
        """One-time lift of old slider-format personal presets into eqfx files; the old file stays as backup."""
        legacy_file = self.window.project_root / "state" / "user" / "audio_presets.json"
        if not legacy_file.exists():
            return
        legacy = self._read_json_map(legacy_file, "user audio_presets.json")
        if not legacy:
            return
        from engine.audio_chain import preset_from_legacy

        self.user_eqfx_dir.mkdir(parents=True, exist_ok=True)
        for name, old in legacy.items():
            dest = self.user_eqfx_dir / f"{_safe_filename(name)}.json"
            if dest.exists():
                continue
            preset = {
                "name": name,
                "description": old.get("description", "Migrated preset."),
                "tts_voice": old.get("tts_voice", ""),
                **preset_from_legacy(old),
            }
            try:
                dest.write_text(json.dumps(preset, indent=2), encoding="utf-8")
            except OSError as e:
                print(f"Preset Migration: Could not write {dest.name}: {e}")

    def _all_presets(self):
        merged = dict(self.builtin_presets)
        merged.update(self.user_presets)  # user files win on name collisions
        return merged

    # ------------------------------------------------------------ UI helpers
    def _section(self, text, color=_ACCENT):
        lab = QLabel(text)
        lab.setStyleSheet(f"color: {color}; font-size: 10px; letter-spacing: 2px; border: none;")
        return lab

    def _spin(self, lo, hi, decimals, step, value):
        spin = QDoubleSpinBox()
        spin.setRange(lo, hi)
        spin.setDecimals(decimals)
        spin.setSingleStep(step)
        spin.setValue(value)
        spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        spin.setFixedHeight(20)
        return spin

    def _vdiv(self):
        """1 px vertical divider for the selection bar."""
        line = QFrame()
        line.setFixedSize(1, 16)
        line.setStyleSheet("background: #2e3a44; border: none;")
        return line

    # ------------------------------------------------------------ build
    def _preset_btn(self, text, width, tooltip, slot):
        """Header preset-bar button: fixed width, slim horizontal padding (the app-wide
        14px button padding wastes header space), standard chrome otherwise."""
        btn = QPushButton(text)
        btn.setFixedWidth(width)
        btn.setToolTip(tooltip)
        btn.setStyleSheet("padding: 5px 6px;")
        btn.clicked.connect(slot)
        return btn

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(PAGE_MARGIN, 6, PAGE_MARGIN, 6)
        outer.setSpacing(BEZEL_GAP)

        # Header preset bar -------------------------------------------
        preset_group, header = make_group(" PRESET ", horizontal=True)
        self.preset_combo = QComboBox()
        self.preset_combo.addItem(_NO_PRESET)
        self.preset_combo.addItems(list(self._all_presets().keys()))
        self.preset_combo.currentTextChanged.connect(self._on_preset_selected)
        header.addWidget(self.preset_combo, 1)
        self.save_preset_btn = self._preset_btn(
            "Save", 44,
            "Overwrite the selected user preset with the current rack (voice included).\n"
            "Built-ins can't be overwritten - Save falls back to Save As.",
            self._save_user_preset)
        header.addWidget(self.save_preset_btn)
        self.save_as_btn = self._preset_btn(
            "Save As", 60,
            "Store the current rack as a (new) user preset under a name you type.\n"
            "Asks before overwriting an existing preset.",
            self._save_user_preset_as)
        header.addWidget(self.save_as_btn)
        self.delete_preset_btn = self._preset_btn(
            "Delete", 56, "Removes the selected user preset. Built-in presets are immutable.",
            self._delete_user_preset)
        self.rename_preset_btn = self._preset_btn(
            "Rename", 56, "Rename the selected user preset. Built-in presets are immutable.",
            self._rename_user_preset)
        header.addWidget(self.rename_preset_btn)
        header.addWidget(self.delete_preset_btn)
        self.import_btn = self._preset_btn(
            "Import", 56,
            "Import preset JSON files (e.g. exported from another eqfx app) into your personal store.",
            self._import_presets)
        header.addWidget(self.import_btn)
        self.export_btn = self._preset_btn(
            "Export", 56,
            "Export the selected preset (or your current rack) to a JSON file.",
            self._export_preset)
        header.addWidget(self.export_btn)
        self.delete_preset_btn.setEnabled(False)
        self.rename_preset_btn.setEnabled(False)
        outer.addWidget(preset_group)

        main = QHBoxLayout()
        main.setSpacing(BEZEL_GAP)
        outer.addLayout(main, 1)

        # LEFT: channel strip -----------------------------------------
        # One FX module bezel; VOICE/DELIVERY/RACK are sub-sections inside it.
        fx_group, fx_v = make_group(" FX ")
        fx_group.setFixedWidth(220)
        main.addWidget(fx_group)
        # Right margin 2: the scrollbar (AsNeeded) hugs the bezel edge. The rows
        # can't shift - strip_inner is fixed-width and left-aligned in the viewport.
        fx_v.setContentsMargins(BEZEL_PAD_X, BEZEL_PAD_TOP, 2, BEZEL_PAD_BOTTOM)
        # The strip scrolls when an accordion panel overflows (rows never squeeze).
        strip_scroll = QScrollArea()
        strip_scroll.setWidgetResizable(True)
        strip_scroll.setFrameShape(QFrame.Shape.NoFrame)
        strip_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        strip_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        strip_scroll.setStyleSheet(
            "QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }"
        )
        # Fixed inner width: the scrollbar (AsNeeded) draws into this reserved gutter
        # instead of shrinking the viewport, so accordion expand/collapse never re-lays
        # out the rows. 220 - 2 border - 2*BEZEL_PAD_X - 6 scrollbar = 192.
        strip_inner = QWidget()
        strip_inner.setFixedWidth(192)
        left_v = QVBoxLayout(strip_inner)
        left_v.setContentsMargins(0, 0, 0, 0)
        left_v.setSpacing(3)
        strip_scroll.setWidget(strip_inner)
        fx_v.addWidget(strip_scroll)
        left_v.addWidget(self._section("VOICE"))
        self.voice_combo = QComboBox()
        self.voice_combo.setToolTip(
            "The synth voice the sandbox renders with and Apply Changes commits.\n"
            "Presets with a bound voice switch this automatically.")
        left_v.addWidget(self.voice_combo)
        self.voice_combo.currentIndexChanged.connect(lambda _i: self._log(
            f" Sandbox voice: '{self.current_voice()}'. Apply Changes to commit it to the engine."))
        left_v.addSpacing(6)
        left_v.addWidget(self._section("DELIVERY"))
        self.volume_slider, _ = self._delivery_row(
            left_v, "volume", -10, 10, 1, lambda v: f"{v:+d}" if v else "0", 0)
        self.volume_slider.setToolTip(
            "Volume trim for this preset (saved into it). 0 = normal game-dialogue loudness.\n"
            "Buried in combat? Raise it. Overpowering the game? Lower it. Commits on Apply Changes.")
        self.speed_slider, _ = self._delivery_row(left_v, "speed", 50, 200, 0.01, "{:.2f}x", 100)
        self.pitch_slider, _ = self._delivery_row(left_v, "pitch", -120, 120, 0.1, "{:+.1f}st", 0)
        left_v.addSpacing(6)
        left_v.addWidget(self._section("RACK"))
        self.fx_rows_layout = QVBoxLayout()
        self.fx_rows_layout.setSpacing(2)
        left_v.addLayout(self.fx_rows_layout)
        self._build_fx_rows()
        left_v.addStretch(1)

        # CENTER: EQ analyzer -----------------------------------------
        eq_group, eq_v = make_group(" EQ / SPECTRUM ")
        main.addWidget(eq_group, 1)
        self.band_count = QLabel(f"0 / {_MAX_EQ_BANDS} bands")
        self.band_count.setStyleSheet(f"color: {_DIM}; font-size: 9px; border: none;")
        eq_v.addWidget(self.band_count, 0, Qt.AlignmentFlag.AlignRight)
        self.canvas = EQCurveCanvas(self)
        self.canvas.setToolTip(
            "Click empty space to add a band. Drag nodes for frequency/gain, mouse wheel for Q,\n"
            "right-click a node for type/remove, drag a node out of the canvas to delete it."
        )
        self.canvas.attach(self._bands)
        eq_v.addWidget(self.canvas, 1)
        # Selection bar: precise numeric entry for the selected band only.
        sel_bar = QWidget()
        bh = QHBoxLayout(sel_bar)
        bh.setContentsMargins(0, 0, 0, 0)
        bh.setSpacing(4)
        self.sel_type = QComboBox()
        self.sel_type.addItems(list(_EQ_TYPES))
        self.sel_type.setFixedWidth(84)
        self.sel_freq = self._spin(20.0, _AXIS_MAX_HZ, 0, 10.0, 1000.0)
        self.sel_gain = self._spin(-18.0, 18.0, 1, 0.5, 0.0)
        self.sel_q = self._spin(0.1, 10.0, 2, 0.1, 0.707)
        self.sel_remove = QPushButton("\u2715")
        self.sel_remove.setFixedSize(18, 18)
        self.sel_remove.setToolTip("Remove this band.")
        self.sel_remove.setCursor(Qt.CursorShape.PointingHandCursor)
        # Inline look: no button chrome, just a glyph that reddens on hover.
        self.sel_remove.setStyleSheet(
            "QPushButton { border: none; background: transparent; color: #9aa4ae; font-size: 12px; }"
            "QPushButton:hover { color: #ff6b6b; background: transparent; }"
        )
        bh.addWidget(self.sel_type)
        bh.addWidget(self._vdiv())
        groups = (("freq", self.sel_freq), ("gain", self.sel_gain), ("q", self.sel_q))
        for i, (text, spin) in enumerate(groups):
            lab = QLabel(text)
            lab.setStyleSheet(f"color: {_DIM}; font-size: 10px; border: none;")
            bh.addWidget(lab)
            bh.addWidget(spin, 1)
            if i < len(groups) - 1:
                bh.addWidget(self._vdiv())
        bh.addWidget(self.sel_remove)
        eq_v.addWidget(sel_bar)
        self.sel_bar = sel_bar
        self.sel_type.currentTextChanged.connect(lambda _t: self._on_sel_bar_changed())
        for spin in (self.sel_freq, self.sel_gain, self.sel_q):
            spin.valueChanged.connect(lambda _v: self._on_sel_bar_changed())
        self.sel_remove.clicked.connect(self._remove_selected_band)
        self.sel_bar.setVisible(False)

        # TRANSPORT (bottom) -------------------------------------------
        # Output readouts fold into the transport strip: no dedicated panel.
        t_group, t_v = make_group(" TEST ")
        outer.addWidget(t_group)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.test_text = QLineEdit("This is a test of how the Dynamic Suit Voice will sound in the game.")
        row.addWidget(self.test_text, 1)
        self.render_btn = QPushButton("Test Voice")
        self.render_btn.clicked.connect(self._run_async_audio_test)
        row.addWidget(self.render_btn)
        self.raw_voice_btn = QPushButton("A/B Dry")
        self.raw_voice_btn.setCheckable(True)
        self.raw_voice_btn.setToolTip(
            "Hear the unprocessed voice: stashes your current rack and loads a\n"
            "neutral one (natural pace, no EQ/FX). Click again to restore it."
        )
        self.raw_voice_btn.toggled.connect(self._toggle_ab_dry)
        row.addWidget(self.raw_voice_btn)
        t_v.addLayout(row)
        status = QHBoxLayout()
        status.setSpacing(6)
        self.lufs_label = self._readout("- LUFS")
        self.lufs_label.setToolTip(
            "General loudness of the render (LUFS, before the game's mix).\n"
            "Buried in combat? Raise the volume slider. Overpowering? Lower it.")
        self.peak_label = self._readout("- PEAK")
        self.peak_label.setToolTip(
            "How close the loudest moment gets to 1.0 (distortion ceiling).\n"
            "0.9-0.99 is normal and safe.")
        status.addWidget(self.lufs_label)
        status.addWidget(self.peak_label)
        self.status_log = QPlainTextEdit()
        self.status_log.setReadOnly(True)
        self.status_log.setFixedHeight(40)  # refined after polish in _fit_console_height
        self.status_log.setStyleSheet(
            f"background-color: #121212; color: {CONSOLE_COLOR}; {MONO} font-size: 11px; "
            "border: 1px solid #262e36; padding: 0 6px;"
        )
        status.addWidget(self.status_log, 1)
        t_v.addLayout(status)
        self._log(" Idle. The first render may take a minute to boot the local speech model.")
        QTimer.singleShot(0, self._fit_console_height)

    def _fit_console_height(self):
        """Fixed height: exactly 3 text lines, zero bottom margin (document margin removed).
        Line height comes from the document layout when measurable - it can differ from fontMetrics."""
        self.status_log.document().setDocumentMargin(0)
        line_h = float(self.status_log.fontMetrics().lineSpacing())
        doc = self.status_log.document()
        last = doc.lastBlock()
        if last.isValid():
            laid_out = doc.documentLayout().blockBoundingRect(last).height()
            if line_h * 0.5 <= laid_out <= line_h * 1.5:  # a single laid-out line: trust the layout
                line_h = laid_out
        self.status_log.setFixedHeight(int(3 * line_h) + 2)  # +2: the 1px top/bottom border

    def _delivery_row(self, layout, label, lo, hi, scale, fmt, default):
        """DAW slider row: dim label, slider, accent readout (double-click to type an exact
        value). `fmt` is a format string or a callable taking the scaled value."""
        row = QHBoxLayout()
        row.setSpacing(6)
        lab = QLabel(label)
        lab.setStyleSheet(f"color: {_DIM}; border: none;")
        lab.setFixedWidth(42)
        row.addWidget(lab)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(lo, hi)
        slider.setValue(default)
        slider.setMinimumWidth(50)
        row.addWidget(slider, 1)
        if isinstance(fmt, str):
            num_fmt = fmt[: fmt.index("}") + 1]

            def fmt_val(v, _f=fmt):
                return _f.format(v)
        else:
            num_fmt = "{}"
            fmt_val = fmt
        val = _ClickableLabel(fmt_val(default * scale))
        val.setStyleSheet(f"color: {_ACCENT}; {MONO} border: none;")
        val.setFixedWidth(48)
        val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(val)
        editor = _ValueEditor()
        editor.setFixedWidth(48)
        editor.setStyleSheet(
            f"color: {_ACCENT}; {MONO} background: #0d1115; "
            "border: 1px solid #2e3a44; padding: 0 2px;"
        )
        editor.setAlignment(Qt.AlignmentFlag.AlignRight)
        editor.hide()
        row.addWidget(editor)
        slider.valueChanged.connect(lambda v: val.setText(fmt_val(v * scale)))

        def _start_edit():
            editor.setText(num_fmt.format(slider.value() * scale))
            val.hide()
            editor.show()
            editor.selectAll()
            editor.setFocus()

        def _commit(*_args):
            if not editor.isVisible():
                return
            try:
                value = max(lo * scale, min(hi * scale, float(editor.text())))
                slider.setValue(int(round(value / scale)))
            except ValueError:
                pass
            editor.hide()
            val.show()

        def _cancel():
            editor.hide()
            val.show()

        val.double_clicked.connect(_start_edit)
        editor.committed.connect(_commit)
        editor.cancelled.connect(_cancel)
        layout.addLayout(row)
        return slider, val

    def _readout(self, text="-"):
        lab = QLabel(text)
        lab.setStyleSheet(
            f"color: {_ACCENT}; {MONO} font-size: 12px; "
            "background: #0d1115; border: 1px solid #262e36; border-radius: 3px; padding: 1px 8px;"
        )
        return lab

    # ------------------------------------------------------------ FX rows
    def _build_fx_rows(self):
        """Row = LED + name + expander; ALL parameters live in the nested panel."""
        self.fx = {}
        for slot, title, primary, params in _FX_CARDS:
            row_w = QFrame()
            row_w.setStyleSheet("QFrame { border: none; }")
            h = QHBoxLayout(row_w)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(6)
            led = QCheckBox()
            led.setFixedWidth(20)
            led.setToolTip(f"Enable {title}")
            name = QLabel(title)
            name.setStyleSheet(f"color: {_DIM}; border: none;")
            h.addWidget(led)
            h.addWidget(name, 1)
            exp = QToolButton()
            exp.setText("\u25b8")
            exp.setFixedWidth(20)
            exp.setCheckable(True)
            exp.setToolTip("Show parameters")
            h.addWidget(exp)
            panel = QWidget()
            pv = QVBoxLayout(panel)
            pv.setContentsMargins(22, 0, 0, 0)
            pv.setSpacing(2)
            spins = {}
            for key, label, lo, hi, dec, step, default in params:
                spin = self._spin(lo, hi, dec, step, default)
                spin.setFixedWidth(88)
                spin.setToolTip(label)
                spin.setEnabled(False)
                spins[key] = spin
                r = QHBoxLayout()
                r.setSpacing(6)
                rl = QLabel(label)
                rl.setStyleSheet(f"color: {_DIM}; font-size: 10px; border: none;")
                r.addWidget(rl, 1)
                r.addWidget(spin)
                pv.addLayout(r)
            panel.setVisible(False)
            self.fx_rows_layout.addWidget(row_w)
            self.fx_rows_layout.addWidget(panel)
            exp.toggled.connect(lambda on, p=panel: self._expand_panel(p, on))
            led.toggled.connect(lambda on, s=slot: self._fx_toggled(s, on))
            self.fx[slot] = {"led": led, "name": name, "spins": spins, "panel": panel,
                             "exp": exp, "primary": primary}

    def _expand_panel(self, panel, on):
        """Accordion: opening one unit's editor closes the others; scroll the opener into view."""
        if on:
            for unit in self.fx.values():
                if unit["panel"] is not panel:
                    unit["exp"].setChecked(False)
            if getattr(self, "strip_scroll", None) is not None:
                self.strip_scroll.ensureWidgetVisible(panel, 0, 10)
        panel.setVisible(on)

    def _fx_toggled(self, slot, on):
        unit = self.fx[slot]
        unit["name"].setStyleSheet(f"color: {TEXT if on else _DIM}; border: none;")
        for spin in unit["spins"].values():
            spin.setEnabled(on)
        if self._initializing or getattr(self, "_loading_rack", False):
            return
        unit["exp"].setChecked(on)  # enabling opens the params; disabling tidies up
        title = next(t for s, t, _p, _pa in _FX_CARDS if s == slot)
        self._log(f" {title} {'enabled' if on else 'disabled'}.")

    # ------------------------------------------------------------ EQ selection bar + canvas callbacks
    def _update_band_count(self):
        self.band_count.setText(f"{len(self._bands)} / {_MAX_EQ_BANDS} bands")

    def _sync_selection_bar(self):
        """Mirror the selected band into the precision bar (hidden when nothing is selected)."""
        has = 0 <= self._sel_band < len(self._bands)
        self.sel_bar.setVisible(has)
        if not has:
            return
        band = self._bands[self._sel_band]
        self._syncing = True
        band_type = str(band.get("type", "peaking"))
        self.sel_type.setCurrentText(band_type if band_type in _EQ_TYPES else "peaking")
        self.sel_freq.setValue(float(band.get("freq", 1000.0)))
        self.sel_gain.setValue(float(band.get("gain_db", 0.0)))
        self.sel_q.setValue(float(band.get("q", 0.707)))
        self._syncing = False

    def _on_sel_bar_changed(self):
        """Precision-bar edit -> band dict + canvas."""
        if self._syncing or not (0 <= self._sel_band < len(self._bands)):
            return
        band = self._bands[self._sel_band]
        band["type"] = self.sel_type.currentText()
        band["freq"] = self.sel_freq.value()
        band["gain_db"] = self.sel_gain.value()
        band["q"] = self.sel_q.value()
        self.canvas.update()

    def _remove_selected_band(self):
        self.on_canvas_delete(self._sel_band)

    def on_canvas_selected(self, index):
        self._sel_band = index
        self._sync_selection_bar()

    def on_canvas_band_edited(self, index):
        """Canvas drag/wheel/menu mutated self._bands[index]; sync the selection bar."""
        if index == self._sel_band:
            self._sync_selection_bar()

    def on_canvas_type_changed(self, index, band_type):
        if 0 <= index < len(self._bands):
            self._bands[index]["type"] = band_type
            self.canvas.update()
            self._sync_selection_bar()

    def on_canvas_reset_q(self, index):
        if 0 <= index < len(self._bands):
            self._bands[index]["q"] = 0.707
            self.canvas.update()
            self._sync_selection_bar()

    def on_canvas_add(self, freq, gain):
        if len(self._bands) >= _MAX_EQ_BANDS:
            self._log(" EQ is at the 10-band limit - remove a band first.")
            return
        freq = int(round(max(20.0, min(_AXIS_MAX_HZ, freq))))
        gain_db = max(-18.0, min(18.0, round(gain * 2.0) / 2.0))
        self._bands.append({"type": "peaking", "freq": freq, "gain_db": gain_db, "q": 0.707})
        self._sel_band = len(self._bands) - 1
        self.canvas.set_selection(self._sel_band)
        self._sync_selection_bar()
        self._update_band_count()
        self.canvas.update()

    def on_canvas_delete(self, index):
        if 0 <= index < len(self._bands):
            self._bands.pop(index)
        self._sel_band = -1
        self.canvas.set_selection(-1)
        self._sync_selection_bar()
        self._update_band_count()
        self.canvas.update()

    # ------------------------------------------------------------ rack state
    def _collect_fx(self):
        fx = {}
        for slot, _title, _primary, params in _FX_CARDS:
            unit = self.fx[slot]
            data = {"enabled": unit["led"].isChecked()}
            for key, *_rest in params:
                data[key] = unit["spins"][key].value()
            if slot == "echo" and self._echo_taps:
                data.update(self._echo_taps)  # preserve tap lists the rows don't edit
            if slot == "radio" and self._radio_bands:
                data["bands"] = self._radio_bands
            fx[slot] = data
        return fx

    def _current_rack(self) -> dict:
        return {
            "speed": round(self.speed_slider.value() * 0.01, 2),
            "pitch_st": round(self.pitch_slider.value() * 0.1, 1),
            "volume": int(self.volume_slider.value()),
            "eq_bands": [dict(b) for b in self._bands],
            "fx": self._collect_fx(),
        }

    def _load_rack(self, preset: dict):
        """Load a preset dict into the widgets (partial presets fall back to neutral)."""
        self._syncing = True
        self.speed_slider.setValue(int(round(float(preset.get("speed", 1.0) or 1.0) * 100)))
        self.pitch_slider.setValue(int(round(float(preset.get("pitch_st", 0.0) or 0.0) * 10)))
        try:
            volume = float(preset.get("volume", 0) or 0)
        except (TypeError, ValueError):
            volume = 0.0
        self.volume_slider.setValue(int(round(max(-10.0, min(10.0, volume)))))
        self._syncing = False
        # Mutate in place: the canvas holds a live reference to this list.
        self._bands[:] = [dict(b) for b in (preset.get("eq_bands") or [])][:_MAX_EQ_BANDS]
        self._sel_band = -1
        self.canvas.set_selection(-1)
        self._sync_selection_bar()
        self._update_band_count()
        src_fx = preset.get("fx") if isinstance(preset.get("fx"), dict) else {}
        self._echo_taps = {}
        self._radio_bands = []
        self._loading_rack = True
        for slot, _title, _primary, params in _FX_CARDS:
            unit = self.fx[slot]
            data = src_fx.get(slot) if isinstance(src_fx.get(slot), dict) else {}
            unit["led"].setChecked(bool(data.get("enabled", False)))
            for key, _lab, _lo, _hi, _dec, _step, default in params:
                unit["spins"][key].setValue(float(data.get(key, default)))
            if slot == "echo" and data.get("taps_ms"):
                self._echo_taps = {"taps_ms": data.get("taps_ms"), "taps_gain": data.get("taps_gain", [])}
            if slot == "radio" and data.get("bands"):
                self._radio_bands = list(data.get("bands"))
        self._loading_rack = False
        self.canvas.update()

    # ------------------------------------------------------------ presets
    def _sync_preset_buttons(self):
        is_user = self.preset_combo.currentText() in self.user_presets
        self.delete_preset_btn.setEnabled(is_user)
        self.rename_preset_btn.setEnabled(is_user)

    # ------------------------------------------------------------ voice roster
    def _voice_catalog(self):
        catalog = {key: {"name": value[0]} for key, value in _BASE_CATALOG.items()}
        model_name = _pocket_model(self.window)
        for voice in tts_utils.list_pocket_voices(self.window.project_root, model_name):
            if voice not in catalog:
                catalog[voice] = {"name": voice}
        return catalog

    def _rebuild_voice_combo(self):
        self.voice_combo.blockSignals(True)
        self.voice_combo.clear()
        for key, info in self._voice_catalog().items():
            self.voice_combo.addItem(info["name"], key)
        current = self.window.user_data.get("tts_voice") or tts_utils.POCKET_DEFAULT_VOICE
        idx = self.voice_combo.findData(current)
        self.voice_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.voice_combo.blockSignals(False)

    def refresh_voices(self):
        """Re-sync the dropdown (clones added or removed in System Environment)."""
        current = self.current_voice()
        self._rebuild_voice_combo()
        if current:
            self.set_voice(current)

    def current_voice(self) -> str:
        return self.voice_combo.currentData() or ""

    def set_voice(self, key: str) -> bool:
        """Move the voice selection to key (preset voice binding)."""
        idx = self.voice_combo.findData(key)
        if idx < 0:
            return False
        self.voice_combo.setCurrentIndex(idx)
        return True

    def _load_initial_state(self):
        """Boot state: the committed active rack wins, else match the committed voice, else Factory."""
        active = None
        if self.active_file.exists():
            try:
                data = json.loads(self.active_file.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "speed" in data:
                    active = data
            except Exception as e:
                print(f"Preset Loader: Failed to parse active_eqfx.json: {e}")
        if active:
            self._load_rack(active)
            self.preset_combo.blockSignals(True)
            name = str(active.get("name", ""))
            idx = self.preset_combo.findText(name)
            self.preset_combo.setCurrentIndex(idx if idx >= 0 else 0)
            self.preset_combo.blockSignals(False)
            self._sync_preset_buttons()
            return
        voice = self.window.user_data.get("tts_voice", "")
        if not voice:
            self.preset_combo.setCurrentText(_FACTORY_PRESET)
            return
        for name, preset in self._all_presets().items():
            if preset.get("tts_voice") == voice:
                self.preset_combo.setCurrentText(name)
                return

    def _on_preset_selected(self, name: str):
        self._sync_preset_buttons()
        p = self._all_presets().get(name)
        if not p:
            return
        self._load_rack(p)
        if self._initializing:
            return
        # A preset is more than EQ/FX: switch the audition voice too (dropdown
        # selection only - Apply Changes commits it to the engine).
        preset_voice = p.get("tts_voice")
        if preset_voice and self.current_voice() != preset_voice:
            if self.set_voice(preset_voice):
                self._log(f" Voice switched to '{preset_voice}'. Apply Changes to commit it to the engine.")
            else:
                self._log(f" Preset voice '{preset_voice}' is not in the roster.")
        desc = str(p.get("description", ""))
        self._log(f" {desc}" if desc else f" Preset '{name}' loaded.")

    def _rebuild_preset_combo(self, select: str = ""):
        self.preset_combo.blockSignals(True)
        current = select or self.preset_combo.currentText()
        self.preset_combo.clear()
        self.preset_combo.addItem(_NO_PRESET)
        self.preset_combo.addItems(list(self._all_presets().keys()))
        idx = self.preset_combo.findText(current)
        self.preset_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.preset_combo.blockSignals(False)
        self._sync_preset_buttons()

    def _save_user_preset(self):
        """Save: overwrite the selected user preset in place. Built-ins/custom fall back to Save As."""
        name = self.preset_combo.currentText()
        if name not in self.user_presets:
            self._log(" Built-ins can't be overwritten - use Save As to store your rack as a new preset.")
            self._save_user_preset_as()
            return
        desc = str(self.user_presets[name].get("description", "") or "User preset.")
        if self._write_user_preset(name, desc):
            self._log(f" Preset '{name}' updated (voice: {self.current_voice() or 'unchanged'}).")

    def _save_user_preset_as(self):
        """Save As: name prompt; an existing user preset is only overwritten after confirmation."""
        name, ok = QInputDialog.getText(self, "Save Preset As", "Preset name:")
        if not ok:
            return
        name = name.strip()
        if not name or name == _NO_PRESET:
            return
        if name in self.builtin_presets and name not in self.user_presets:
            QMessageBox.critical(self, "Save Preset As", f"'{name}' is a built-in preset; pick another name.")
            return
        if name in self.user_presets and QMessageBox.question(
            self, "Save Preset As", f"'{name}' already exists. Overwrite it?"
        ) != QMessageBox.StandardButton.Yes:
            return
        if self._write_user_preset(name, "User preset."):
            self._rebuild_preset_combo(select=name)
            self._log(f" Preset '{name}' saved (voice: {self.current_voice() or 'unchanged'}).")

    def _write_user_preset(self, name: str, description: str) -> bool:
        """Write the tab's current rack as a user preset file; True on success."""
        preset = {
            "name": name,
            "description": description,
            "tts_voice": self.current_voice(),
            **self._current_rack(),
        }
        dest = self.user_eqfx_dir / f"{_safe_filename(name)}.json"
        try:
            self.user_eqfx_dir.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(preset, indent=2), encoding="utf-8")
        except OSError as e:
            QMessageBox.critical(self, "Save Preset", f"Could not write the preset file:\n{e}")
            return False
        self.user_presets = self._load_user_presets()
        return True

    def _delete_user_preset(self):
        name = self.preset_combo.currentText()
        if name not in self.user_presets:
            return
        if QMessageBox.question(self, "Delete Preset", f"Delete user preset '{name}'?") != QMessageBox.StandardButton.Yes:
            return
        dest = self.user_eqfx_dir / f"{_safe_filename(name)}.json"
        try:
            if dest.exists():
                dest.unlink()
        except OSError as e:
            QMessageBox.critical(self, "Delete Preset", f"Could not delete the preset file:\n{e}")
            return
        self.user_presets = self._load_user_presets()
        self._rebuild_preset_combo(select=_NO_PRESET)
        self._log(f" Preset '{name}' deleted.")

    def _rename_user_preset(self):
        """Rename the selected user preset: rewrite the file under the new name, drop the old."""
        name = self.preset_combo.currentText()
        if name not in self.user_presets:
            return
        new_name, ok = QInputDialog.getText(self, "Rename Preset", "New name:", text=name)
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == _NO_PRESET or new_name == name:
            return
        if new_name in self.builtin_presets and new_name not in self.user_presets:
            QMessageBox.critical(self, "Rename Preset", f"'{new_name}' is a built-in preset; pick another name.")
            return
        if new_name in self.user_presets and QMessageBox.question(
            self, "Rename Preset", f"'{new_name}' already exists. Overwrite it?"
        ) != QMessageBox.StandardButton.Yes:
            return
        src = self.user_eqfx_dir / f"{_safe_filename(name)}.json"
        dest = self.user_eqfx_dir / f"{_safe_filename(new_name)}.json"
        try:
            self.user_eqfx_dir.mkdir(parents=True, exist_ok=True)
            data = dict(self.user_presets[name])
            data["name"] = new_name
            dest.write_text(json.dumps(data, indent=2), encoding="utf-8")
            if src.exists() and src != dest:
                src.unlink()
        except OSError as e:
            QMessageBox.critical(self, "Rename Preset", f"Could not rename the preset file:\n{e}")
            return
        self.user_presets = self._load_user_presets()
        self._rebuild_preset_combo(select=new_name)
        self._log(f" Preset '{name}' renamed to '{new_name}'.")

    # ------------------------------------------------------ import / export
    def _import_presets(self):
        paths, _filter = QFileDialog.getOpenFileNames(
            self, "Import Presets", "", "Preset JSON (*.json);;All files (*)")
        if not paths:
            return
        imported = 0
        for path in paths:
            imported += 1 if self._import_preset_file(Path(path)) else 0
        if imported:
            self.user_presets = self._load_user_presets()
            self._rebuild_preset_combo()
            self._log(f" Imported {imported} preset(s) into your personal store.")

    def _import_preset_file(self, path: Path) -> bool:
        """Tolerant import: eq/fx land exactly, missing delivery defaults to neutral,
        unknown keys (e.g. spatial) are ignored. Returns True on success."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            QMessageBox.critical(self, "Import Preset", f"Could not parse {path.name}:\n{e}")
            return False
        if not isinstance(data, dict) or not (data.get("eq_bands") or data.get("fx")):
            QMessageBox.critical(self, "Import Preset", f"{path.name} is not a preset file (no eq_bands or fx).")
            return False
        name = str(data.get("name") or path.stem).strip() or path.stem
        if name in self.builtin_presets and name not in self.user_presets:
            name = f"{name} (imported)"
        dest = self.user_eqfx_dir / f"{_safe_filename(name)}.json"
        if dest.exists() and QMessageBox.question(
            self, "Import Preset", f"'{name}' already exists. Overwrite it?"
        ) != QMessageBox.StandardButton.Yes:
            return False
        try:
            speed = float(data.get("speed", 1.0) or 1.0)
            pitch = float(data.get("pitch_st", 0.0) or 0.0)
        except (TypeError, ValueError):
            speed, pitch = 1.0, 0.0
        try:
            volume = float(data.get("volume", 0.0) or 0.0)
        except (TypeError, ValueError):
            volume = 0.0
        preset = {
            "name": name,
            "description": str(data.get("description") or "Imported preset."),
            "tts_voice": str(data.get("tts_voice") or ""),
            "speed": max(0.5, min(2.0, speed)),
            "pitch_st": max(-12.0, min(12.0, pitch)),
            "volume": max(-10.0, min(10.0, volume)),
            "eq_bands": data.get("eq_bands") if isinstance(data.get("eq_bands"), list) else [],
            "fx": data.get("fx") if isinstance(data.get("fx"), dict) else {},
        }
        try:
            self.user_eqfx_dir.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(preset, indent=2), encoding="utf-8")
        except OSError as e:
            QMessageBox.critical(self, "Import Preset", f"Could not write the preset file:\n{e}")
            return False
        self.user_presets = self._load_user_presets()
        return True

    def _export_payload(self) -> dict:
        """The dict to export: the selected preset, or the current rack for a custom selection."""
        name = self.preset_combo.currentText()
        preset = self._all_presets().get(name)
        if preset and name != _NO_PRESET:
            payload = dict(preset)
            payload["name"] = name  # built-ins carry the name in the store key, not the dict
            return payload
        return {"name": "custom rack", "description": "Exported rack.",
                "tts_voice": self.current_voice(), **self._current_rack()}

    def _export_preset(self):
        payload = self._export_payload()
        target, _filter = QFileDialog.getSaveFileName(
            self, "Export Preset", _safe_filename(str(payload.get("name", "preset"))) + ".json",
            "Preset JSON (*.json);;All files (*)")
        if not target:
            return
        payload["name"] = Path(target).stem  # keep the stored name in sync with the file
        try:
            Path(target).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as e:
            QMessageBox.critical(self, "Export Preset", f"Could not write the file:\n{e}")
            return
        self._log(f" Preset exported to {target}.")

    def _toggle_ab_dry(self, on: bool):
        """A/B bypass: stash the current rack and load neutral; toggle off restores it."""
        if on:
            self._ab_stash = self._current_rack()
            self._load_rack({})
            self._log(" A/B dry: neutral rack loaded. Press Test Voice to hear the unprocessed voice.")
        elif getattr(self, "_ab_stash", None) is not None:
            self._load_rack(self._ab_stash)
            self._ab_stash = None
            self._log(" A/B dry: your previous rack is restored.")

    def commit_active_preset(self, voice: str = ""):
        """Write the tab's current rack to active_eqfx.json (the engine live-reloads it on Apply)."""
        name = self.preset_combo.currentText()
        preset = {
            "name": name if name != _NO_PRESET else "custom",
            "description": "Active render rack.",
            "tts_voice": voice,
            **self._current_rack(),
        }
        try:
            self.active_file.parent.mkdir(parents=True, exist_ok=True)
            self.active_file.write_text(json.dumps(preset, indent=2), encoding="utf-8")
        except OSError as e:
            QMessageBox.critical(self, "Apply Changes", f"Could not write the active preset:\n{e}")
            return

    # ------------------------------------------------------------ sandbox test
    def _run_async_audio_test(self):
        self.render_btn.setEnabled(False)
        # Capture ALL widget state on the GUI thread before the worker starts:
        # Qt widgets must never be touched from a worker thread (Qt6Gui crashes).
        self._test_inputs = {
            "text_payload": self.test_text.text().strip(),
            "preset": self._current_rack(),
            "voice": self.current_voice(),
        }
        self._log(" Starting background voice synthesis sequence...")
        self._test_thread = FunctionThread(self._audio_test_job, self)
        self._test_thread.log_line.connect(self._log)
        self._test_thread.finished_ok.connect(self._test_finished_ok)
        self._test_thread.failed.connect(self._test_failed)
        self._test_thread.start()

    def _audio_test_job(self):
        from engine.config import SuitVoiceConfig
        from engine import tts_utils
        from engine import audio_chain
        from engine.audio_chain import render_chain, write_wav_float

        inputs = getattr(self, "_test_inputs", {})
        text_payload = inputs.get("text_payload", "")
        preset = inputs.get("preset") or {}
        voice = inputs.get("voice") or tts_utils.POCKET_DEFAULT_VOICE

        self._test_thread.emit_log(" Connecting to Pocket TTS Engine...")
        heavy_cfg = SuitVoiceConfig(init_llm=False)

        cache_dir = self.window.project_root / "state" / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        final_wav = cache_dir / "sandbox_processed.wav"
        self._test_thread.emit_log(f" Synthesizing vocal wave footprints ({voice})...")
        audio, sr = tts_utils.pocket_generate(
            heavy_cfg.pocket_model_name, heavy_cfg.root_dir, text_payload, voice,
            max_tokens=heavy_cfg.pocket_tts_max_tokens,
        )

        self._test_thread.emit_log(" Rendering through the EQ + FX rack and loudness chain...")
        audio, sr = render_chain(
            audio, sr, preset,
            loudness_target=heavy_cfg.voice_effects.get("loudness_target"),
            true_peak=float(heavy_cfg.voice_effects.get("loudness_true_peak", 5.0)))
        write_wav_float(final_wav, audio, sr)

        stats = {"lufs": round(audio_chain.integrated_loudness(audio, sr), 1),
                 "peak": round(float(np.max(np.abs(audio))), 2)}
        ffplay_bin = str(heavy_cfg.ffmpeg_path.parent / "ffplay.exe")
        self._test_thread.emit_log(" Footprints generated successfully! Triggering playback stream...")
        # Playback only: attenuate by the game suit-voice bus's amount so the
        # sandbox approximates in-game loudness (the written wav stays untouched).
        play_wav = cache_dir / "sandbox_playback.wav"
        write_wav_float(play_wav, audio * (10.0 ** (TESTER_MIX_ATTENUATION_DB / 20.0)), sr)
        subprocess.run([
            ffplay_bin, "-nodisp", "-autoexit", str(play_wav)
        ], check=True, creationflags=_CREATE_NO_WINDOW)
        self._test_thread.emit_log(" Playback loop finished smoothly.")
        return stats

    def _test_finished_ok(self, result):
        self.render_btn.setEnabled(True)
        stats = result if isinstance(result, dict) else {}
        self.lufs_label.setText(f"{stats.get('lufs', '-')} LUFS")
        self.peak_label.setText(f"{stats.get('peak', '-')} peak")

    def _test_failed(self, error: str):
        self.render_btn.setEnabled(True)
        self._log(f"\n ERROR: Synthesis pipeline failed:\n {error}")
        QMessageBox.critical(self, "Sandbox Failure", f"Vocal processing chain error:\n{error}")

    # ------------------------------------------------------------ logging
    def _log(self, message: str):
        """Append without a trailing separator (appendPlainText's phantom empty line otherwise
        eats one of the console's 3 slots)."""
        cursor = self.status_log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if self.status_log.toPlainText():
            cursor.insertBlock()
        cursor.insertText(message)
        self.status_log.setTextCursor(cursor)
        self.status_log.ensureCursorVisible()
        self._center_if_short()

    def _center_if_short(self):
        """A lone line in the 3-slot console reads vertically centered: a pad blank above
        pushes it into the middle slot. The pad self-removes the moment a second line
        arrives (2 lines can't half-center in 3 slots), so longer content scrolls normal."""
        edit = self.status_log
        doc = edit.document()
        if doc.blockCount() > 1 and not doc.firstBlock().text():
            pad = QTextCursor(doc)  # drop the previous centering blank
            pad.movePosition(QTextCursor.MoveOperation.NextBlock, QTextCursor.MoveMode.KeepAnchor)
            pad.removeSelectedText()
        if doc.blockCount() == 1 and doc.firstBlock().length() > 1:
            QTextCursor(doc).insertBlock()  # pad: the single line lands in the middle slot
