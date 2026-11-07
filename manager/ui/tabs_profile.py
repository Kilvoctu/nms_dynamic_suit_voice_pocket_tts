# manager/ui/tabs_profile.py - user identity, briefing text and prompt preferences.
import html
import json

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from engine import prompt_presets
from ui.style import BEZEL_GAP, HINT_QSS, PAGE_MARGIN, make_group

from ui.preset_manager import PresetManagerDialog


class ProfileTab(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        user_data = window.user_data
        phrase_settings = user_data.get("phrase_settings", {})

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN)
        root_layout.setSpacing(BEZEL_GAP)

        # Panel 1: iteration identity -------------------------------------
        identity, identity_layout = make_group(" ITERATION IDENTITY REGISTRATION ")
        root_layout.addWidget(identity)
        row = QHBoxLayout()
        identity_layout.addLayout(row)
        row.addWidget(QLabel("Iteration Name:"))
        self.name_edit = QLineEdit(user_data.get("player_name", "Interloper"))
        self.name_edit.textChanged.connect(self._refresh_briefing_text)
        row.addWidget(self.name_edit)

        # Panel 2: live briefing ------------------------------------------
        briefing, briefing_layout = make_group(" EXOSUIT CORE SYSTEM BRIEFING ")
        root_layout.addWidget(briefing, 1)
        self.briefing_label = QLabel("")
        self.briefing_label.setTextFormat(Qt.TextFormat.RichText)  # per-paragraph margins
        self.briefing_label.setWordWrap(True)
        self.briefing_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        briefing_layout.addWidget(self.briefing_label)

        # Panel 3: companion preferences -----------------------------------
        prefs, prefs_layout = make_group(" COMPANION PREFERENCES ")
        root_layout.addWidget(prefs)
        self._presets = prompt_presets.load_presets(window.project_root)

        grid = QGridLayout()
        grid.setColumnStretch(3, 1)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        prefs_layout.addLayout(grid)

        def _desc_label(ignored: bool):
            lbl = QLabel("")
            lbl.setStyleSheet(HINT_QSS)
            if ignored:
                lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            return lbl

        grid.addWidget(QLabel("AI Core:"), 0, 0)
        self.base_combo = QComboBox()
        self.base_combo.setFixedWidth(220)
        self.base_combo.setToolTip(
            "The core prompt shaping the AI's personality - 'Default' is the\n"
            "built-in prompt; other entries are your custom variants (Manage to edit).\n"
            "Switching takes effect when you press Apply Changes."
        )
        grid.addWidget(self.base_combo, 0, 1)
        self.manage_btn = QPushButton("Manage")
        self.manage_btn.setToolTip("Inspect prompt texts, manage custom presets and test tone + verbiage combinations.")
        self.manage_btn.clicked.connect(self._manage_prompts)
        grid.addWidget(self.manage_btn, 0, 3, Qt.AlignmentFlag.AlignRight)
        self.base_desc = _desc_label(ignored=False)
        grid.addWidget(self.base_desc, 0, 2)

        grid.addWidget(QLabel("Tone Profile:"), 1, 0)
        self.tone_combo = QComboBox()
        self.tone_combo.setFixedWidth(220)
        self._populate_combo(self.tone_combo, "tones", window.available_tones)
        self.tone_combo.setCurrentText(phrase_settings.get("tone", "Standard"))
        grid.addWidget(self.tone_combo, 1, 1)
        self.tone_desc = _desc_label(ignored=True)
        grid.addWidget(self.tone_desc, 1, 2, 1, 2)

        grid.addWidget(QLabel("Verbiage Style:"), 2, 0)
        self.wordiness_combo = QComboBox()
        self.wordiness_combo.setFixedWidth(220)
        self._populate_combo(self.wordiness_combo, "wordiness", window.available_wordiness)
        self.wordiness_combo.setCurrentText(phrase_settings.get("wordiness", "Standard"))
        grid.addWidget(self.wordiness_combo, 2, 1)
        self.wordiness_desc = _desc_label(ignored=True)
        grid.addWidget(self.wordiness_desc, 2, 2, 1, 2)

        self.base_combo.currentTextChanged.connect(self._on_base_changed)
        self.tone_combo.currentTextChanged.connect(lambda _t: self.update_live_descriptions())
        self.wordiness_combo.currentTextChanged.connect(lambda _t: self.update_live_descriptions())
        self._rebuild_base_combo()
        self._refresh_briefing_text()

    # ---------------------------------------------------------------- helpers
    def _rebuild_base_combo(self):
        """Fill the core combo with 'Default' + the user's base prompt variants,
        selecting the active one. Re-run after the Manage dialog closes."""
        active = self._presets.get(prompt_presets.BASE_ACTIVE_KEY, prompt_presets.ACTIVE_DEFAULT)
        self.base_combo.blockSignals(True)
        self.base_combo.clear()
        self.base_combo.addItem(prompt_presets.ACTIVE_DEFAULT)
        base_names = list(self._presets.get(prompt_presets.BASE_SECTION, {}))
        if base_names:
            self.base_combo.insertSeparator(self.base_combo.count())
            self.base_combo.addItems(base_names)
        self.base_combo.setCurrentText(active)
        self.base_combo.blockSignals(False)
        self._update_base_desc()

    def _on_base_changed(self, name: str):
        """Selection only - the pick commits to disk on Apply Changes, like
        tone/verbiage. The combo's current text is the pending choice."""
        self._update_base_desc()

    def _update_base_desc(self):
        name = self.base_combo.currentText()
        entry = self._presets.get(prompt_presets.BASE_SECTION, {}).get(name, {})
        desc = str(entry.get("description", "")).strip()
        if not desc:
            desc = "Built-in prompt." if name == prompt_presets.ACTIVE_DEFAULT else name
        self.base_desc.setToolTip(desc)
        self._schedule_elide()

    def _populate_combo(self, combo, section, builtin_names):
        """Fill a combo with built-ins, a separator, then this user's presets."""
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(list(builtin_names))
        user_names = [n for n in self._presets.get(section, {}) if n not in builtin_names]
        if user_names:
            combo.insertSeparator(combo.count())
            combo.addItems(user_names)
        combo.blockSignals(False)

    def _refresh_combos(self):
        """Re-read user presets from disk and rebuild all combos, keeping
        the current selection when it still exists (else fall back to Standard)."""
        self._presets = prompt_presets.load_presets(self.window.project_root)
        # A pending (unapplied) core pick survives the rebuild when it still exists.
        pending_core = self.base_combo.currentText()
        self._rebuild_base_combo()
        if self.base_combo.findText(pending_core) >= 0:
            self.base_combo.setCurrentText(pending_core)
        for combo, builtin, section in (
            (self.tone_combo, self.window.available_tones, "tones"),
            (self.wordiness_combo, self.window.available_wordiness, "wordiness"),
        ):
            previous = combo.currentText()
            self._populate_combo(combo, section, builtin)
            if combo.findText(previous) >= 0:
                combo.setCurrentText(previous)
            else:
                combo.setCurrentText("Standard")
        self.update_live_descriptions()

    def _manage_prompts(self):
        """Open the unified prompt manager; refresh combos when it closes."""
        dlg = PresetManagerDialog(
            self.window.project_root,
            tone=self.tone_combo.currentText(),
            wordiness=self.wordiness_combo.currentText(),
            on_core_pending=self._apply_pending_core,
            parent=self.window,
        )
        if dlg.exec():
            self._refresh_combos()

    def _apply_pending_core(self, name: str):
        """Manage dialog's Activate: update the pending combo pick (no disk write)."""
        self.base_combo.setCurrentText(name or prompt_presets.ACTIVE_DEFAULT)

    def builtin_entry(self, section, key):
        """Return a built-in {prompt, description} entry from prompt_data.json ({} if absent)."""
        try:
            prompt_file = self.window.project_root / "assets" / "data" / "prompt_data.json"
            with open(prompt_file, "r", encoding="utf-8") as f:
                pdata = json.load(f)
            return pdata.get(section, {}).get(key, {})
        except Exception:
            return {}

    def _refresh_briefing_text(self):
        current_name = self.name_edit.text().strip() or "..."
        # (text, bottom margin): full break between sections, half break between steps.
        paras = (
            (f"Greetings {html.escape(current_name)}!", 14),
            ("Welcome to your Dynamic Suit Voice diagnostic dashboard.", 14),
            ("All settings shape your suit's voice and personality. Apply Changes commits them, "
             "and the running engine picks them up automatically.", 14),
            ("QUICK-START FLIGHT MANUAL:", 4),
            ("1. USER PROFILE. Name your iteration. Choose your AI's narrative behaviors. Manage "
             "opens the preset editor: custom tone/verbiage presets, the core prompt with its "
             "persona distiller, and a combination test on a real in-game phrase. Descriptions "
             "under the dropdowns update live as you switch presets.", 8),
            ("2. VOICE MODIFIER. Pick the synthesis voice and character presets. Fine-tune the "
             "EQ/FX rack: drag the EQ nodes, click empty space to add a band, right-click a node "
             "for type and removal. Trim the volume slider if lines ride too quiet or too loud in "
             "game. Test Voice renders the current rack in the sandbox; A/B Dry plays the raw "
             "voice for comparison. Save, Save As, Rename, Import and Export keep your own "
             "presets one click away.", 8),
            ("3. SYSTEM ENVIRONMENT. Clone voices from a clean sample (10 to 30 seconds, one "
             "speaker, no music); the first clone adds a one-time model sign-in and download. "
             "Wipe Cache and Unused Model is the emergency exit, and a wiped cache refills itself "
             "on the next boot. Watch the live pipeline log.", 14),
            ("FLIGHT NOTES:", 4),
            ("Apply Changes saves to disk at any time while keeping the testing sandbox open. "
             "Closing the window hides it to the tray; the engine keeps running. The tray icon's "
             "Exit stops the engine and quits.", 0),
        )
        self.briefing_label.setText("".join(
            f'<p style="margin: 0 0 {margin}px 0">{text}</p>' for text, margin in paras))
        self.update_live_descriptions()

    def update_live_descriptions(self):
        """Show the selected entries' descriptions (built-ins first, then user presets)."""
        for label, section, combo in (
            (self.tone_desc, "tones", self.tone_combo),
            (self.wordiness_desc, "wordiness", self.wordiness_combo),
        ):
            key = combo.currentText()
            entry = self.builtin_entry(section, key) or self._presets.get(section, {}).get(key, {})
            desc = entry.get("description", "") if isinstance(entry, dict) else ""
            label.setToolTip(desc)
        self._schedule_elide()

    def _elide_descs(self):
        """Fit the one-line descriptions by pixel width. Row 1 stops short of
        Manage; rows 2-3 span to the group edge and elide to their real width."""
        if not self.manage_btn.isVisible():
            return
        fm = self.base_desc.fontMetrics()
        left = self.base_desc.mapTo(self, self.base_desc.rect().topLeft()).x()
        manage_left = self.manage_btn.mapTo(self, self.manage_btn.rect().topLeft()).x()
        row_limit = max(60, manage_left - left - 12)
        self.base_desc.setText(
            fm.elidedText(self.base_desc.toolTip(), Qt.TextElideMode.ElideRight, row_limit))
        for lbl in (self.tone_desc, self.wordiness_desc):
            lbl.setText(fm.elidedText(lbl.toolTip(), Qt.TextElideMode.ElideRight, max(60, lbl.width() - 6)))

    def _schedule_elide(self):
        QTimer.singleShot(0, self._elide_descs)

    def showEvent(self, event):
        super().showEvent(event)
        self._schedule_elide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._schedule_elide()
