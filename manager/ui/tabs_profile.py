# manager/ui/tabs_profile.py - user identity, briefing text and prompt preferences.
import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget


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

        row1 = QHBoxLayout()
        prefs_layout.addLayout(row1)
        row1.addWidget(QLabel("Companion AI Tone Profile:"))
        self.tone_combo = QComboBox()
        self.tone_combo.addItems(window.available_tones)
        self.tone_combo.setCurrentText(phrase_settings.get("tone", "Standard"))
        row1.addWidget(self.tone_combo)
        self.tone_desc = QLabel("")
        self.tone_desc.setStyleSheet("color: #8a8a8a; font-style: italic;")
        row1.addWidget(self.tone_desc, 1)

        row2 = QHBoxLayout()
        prefs_layout.addLayout(row2)
        row2.addWidget(QLabel("Companion AI Verbiage Style:"))
        self.wordiness_combo = QComboBox()
        self.wordiness_combo.addItems(window.available_wordiness)
        self.wordiness_combo.setCurrentText(phrase_settings.get("wordiness", "Standard"))
        row2.addWidget(self.wordiness_combo)
        self.wordiness_desc = QLabel("")
        self.wordiness_desc.setStyleSheet("color: #8a8a8a; font-style: italic;")
        row2.addWidget(self.wordiness_desc, 1)

        self.tone_combo.currentTextChanged.connect(lambda _t: self.update_live_descriptions())
        self.wordiness_combo.currentTextChanged.connect(lambda _t: self.update_live_descriptions())
        self._refresh_briefing_text()

    # ---------------------------------------------------------------- helpers
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
        try:
            prompt_file = self.window.project_root / "data" / "prompt_data.json"
            if not prompt_file.exists():
                return
            with open(prompt_file, "r", encoding="utf-8") as f:
                pdata = json.load(f)
            tone_info = pdata.get("tones", {}).get(self.tone_combo.currentText(), {})
            word_info = pdata.get("wordiness", {}).get(self.wordiness_combo.currentText(), {})
            t_desc = tone_info.get("description", "") if isinstance(tone_info, dict) else ""
            w_desc = word_info.get("description", "") if isinstance(word_info, dict) else ""
            self.tone_desc.setText(f"\u21b3 Profile: {t_desc}")
            self.wordiness_desc.setText(f"\u21b3 Profile: {w_desc}")
        except Exception:
            pass
