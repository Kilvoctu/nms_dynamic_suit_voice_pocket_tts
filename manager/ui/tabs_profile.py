# manager/ui/tabs_profile.py - user identity, briefing text and prompt preferences.
import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from modular import prompt_presets
from ui.preset_manager import PresetManagerDialog


class ProfileTab(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        user_data = window.user_data
        phrase_settings = user_data.get("phrase_settings", {})

        root_layout = QVBoxLayout(self)

        # Panel 1: iteration identity -------------------------------------
        identity = QGroupBox(" Iteration Identity Registration ")
        root_layout.addWidget(identity)
        identity_layout = QVBoxLayout(identity)
        row = QHBoxLayout()
        identity_layout.addLayout(row)
        row.addWidget(QLabel("Iteration Name:"))
        self.name_edit = QLineEdit(user_data.get("player_name", "Interloper"))
        self.name_edit.textChanged.connect(self._refresh_briefing_text)
        row.addWidget(self.name_edit)

        # Panel 2: live briefing ------------------------------------------
        briefing = QGroupBox(" Exosuit Core System Briefing ")
        root_layout.addWidget(briefing, 1)
        briefing_layout = QVBoxLayout(briefing)
        self.briefing_label = QLabel("")
        self.briefing_label.setWordWrap(True)
        self.briefing_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        briefing_layout.addWidget(self.briefing_label)

        # Panel 3: companion preferences -----------------------------------
        prefs = QGroupBox(" Companion Preferences ")
        root_layout.addWidget(prefs)
        prefs_layout = QVBoxLayout(prefs)
        self._presets = prompt_presets.load_presets(window.project_root)

        row1 = QHBoxLayout()
        prefs_layout.addLayout(row1)
        row1.addWidget(QLabel("Companion AI Tone Profile:"))
        self.tone_combo = QComboBox()
        self._populate_combo(self.tone_combo, "tones", window.available_tones)
        self.tone_combo.setCurrentText(phrase_settings.get("tone", "Standard"))
        row1.addWidget(self.tone_combo)
        row1.addStretch(1)
        manage_btn = QPushButton("Manage")
        manage_btn.setToolTip("Inspect prompt texts, manage custom presets and test tone + verbiage combinations.")
        manage_btn.clicked.connect(self._manage_prompts)
        row1.addWidget(manage_btn)
        self.tone_desc = QLabel("")
        self.tone_desc.setStyleSheet("color: #8a8a8a; font-style: italic;")
        self.tone_desc.setWordWrap(True)
        self.tone_desc.setContentsMargins(24, 0, 0, 0)
        prefs_layout.addWidget(self.tone_desc)

        row2 = QHBoxLayout()
        prefs_layout.addLayout(row2)
        row2.addWidget(QLabel("Companion AI Verbiage Style:"))
        self.wordiness_combo = QComboBox()
        self._populate_combo(self.wordiness_combo, "wordiness", window.available_wordiness)
        self.wordiness_combo.setCurrentText(phrase_settings.get("wordiness", "Standard"))
        row2.addWidget(self.wordiness_combo)
        row2.addStretch(1)
        self.wordiness_desc = QLabel("")
        self.wordiness_desc.setStyleSheet("color: #8a8a8a; font-style: italic;")
        self.wordiness_desc.setWordWrap(True)
        self.wordiness_desc.setContentsMargins(24, 0, 0, 0)
        prefs_layout.addWidget(self.wordiness_desc)

        self.tone_combo.currentTextChanged.connect(lambda _t: self.update_live_descriptions())
        self.wordiness_combo.currentTextChanged.connect(lambda _t: self.update_live_descriptions())
        self._refresh_briefing_text()

    # ---------------------------------------------------------------- helpers
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
        """Re-read user presets from disk and rebuild both combos, keeping
        the current selection when it still exists (else fall back to Standard)."""
        self._presets = prompt_presets.load_presets(self.window.project_root)
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
            parent=self.window,
        )
        if dlg.exec():
            self._refresh_combos()

    def builtin_entry(self, section, key):
        """Return a built-in {prompt, description} entry from prompt_data.json ({} if absent)."""
        try:
            prompt_file = self.window.project_root / "data" / "prompt_data.json"
            with open(prompt_file, "r", encoding="utf-8") as f:
                pdata = json.load(f)
            return pdata.get(section, {}).get(key, {})
        except Exception:
            return {}

    def _refresh_briefing_text(self):
        current_name = self.name_edit.text().strip() or "..."
        self.briefing_label.setText(
            f"Greetings {current_name}!\n\n"
            "Welcome to your Dynamic Suit Voice diagnostic dashboard.\n\n"
            "This management utility directly bridges your customized preferences with the local "
            "voice synthesis pipeline. Adjusting settings on these tabs allows you to tailor your suit's "
            "personality and acoustic metrics cleanly before (or during) deployment.\n\n"
            "QUICK-START FLIGHT MANUAL:\n"
            "\u2022 User Profile: Assign your personalized iteration designation and choose your AI's narrative behaviors.\n"
            "\u2022 Audio Modifiers: Fine-tune post-processing filters or test customized presets inside the live sandbox.\n"
            "\u2022 Voice Model: Swap out baseline voice models or safely manage local hard drive storage footprints.\n"
            "\u2022 System Environment: Configure game directory pathways or adjust background polling refresh scales.\n\n"
            "Choose 'Cancel' to close without saving any further changes.\n"
            "Choose 'Save & Close' to save choices to disk and exit the Manager.\n"
            "Choose 'Apply Changes' at any time to save choices to disk while keeping your testing sandbox open."
        )
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
            label.setText(f"\u21b3 Profile: {desc}")
