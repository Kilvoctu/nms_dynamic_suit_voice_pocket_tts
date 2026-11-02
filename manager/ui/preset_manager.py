# manager/ui/preset_manager.py - unified prompt preset manager with combination test.
import json
from pathlib import Path

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from modular import prompt_presets
from ui.workers import FunctionThread


class PresetSectionEditor(QWidget):
    """Two-pane editor for one prompt section: built-ins stay read-only, user
    presets (user/prompt_presets.json) can be created, edited and deleted."""

    def __init__(self, project_root, section, presets, current_name="", parent=None):
        super().__init__(parent)
        self.project_root = project_root
        self.section = section
        self.presets = presets  # shared dict across editors so saves never clobber each other
        self.builtin = self._load_builtin()
        self.current = current_name if self._is_user(current_name) else ""
        self._dirty = False
        self._last_row = 0
        self._build_ui()
        self._reload_list(select=current_name)

    # ------------------------------------------------------------------ data
    def _load_builtin(self) -> dict:
        """Read this section's built-in entries from the tracked prompt_data.json."""
        try:
            path = self.project_root / "data" / "prompt_data.json"
            raw = json.loads(path.read_text(encoding="utf-8"))
            entries = raw.get(self.section, {})
            return {k: v for k, v in entries.items() if isinstance(v, dict)}
        except Exception:
            return {}

    def _is_user(self, name: str) -> bool:
        """True when the name is a user preset (never a built-in)."""
        return bool(name) and name in self.presets.get(self.section, {})

    def is_dirty(self) -> bool:
        """True when the edit pane holds unsaved changes."""
        return self._dirty

    def _set_pane_editable(self, editable: bool = True):
        """Unlock or lock all three edit fields (built-ins are view-only)."""
        self.name_edit.setReadOnly(not editable)
        self.desc_edit.setReadOnly(not editable)
        self.prompt_edit.setReadOnly(not editable)

    # ------------------------------------------------------------------- ui
    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        header = self._header_widget()
        if header is not None:
            outer.addWidget(header)
        body = QHBoxLayout()
        outer.addLayout(body, 1)

        self.list_widget = QListWidget()
        self.list_widget.currentTextChanged.connect(self._on_select)
        body.addWidget(self.list_widget, 1)

        pane = QWidget()
        form = QVBoxLayout(pane)
        form.setContentsMargins(8, 0, 0, 0)
        form.addWidget(QLabel("Name:"))
        self.name_edit = QLineEdit()
        form.addWidget(self.name_edit)
        form.addWidget(QLabel("Description:"))
        self.desc_edit = QLineEdit()
        form.addWidget(self.desc_edit)
        form.addWidget(QLabel("Prompt:"))
        self.prompt_edit = QPlainTextEdit()
        form.addWidget(self.prompt_edit, 1)
        self.hint_label = QLabel("")
        self.hint_label.setStyleSheet("color: #8a8a8a; font-style: italic;")
        self.hint_label.setWordWrap(True)
        form.addWidget(self.hint_label)
        body.addWidget(pane, 2)

        btn_row = QHBoxLayout()
        self.new_btn = QPushButton("New Preset")
        self.new_btn.clicked.connect(self._on_new)
        self.dup_btn = QPushButton("Duplicate to Preset")
        self.dup_btn.clicked.connect(self._on_duplicate)
        self.save_btn = QPushButton("Save")
        self.save_btn.clicked.connect(self._on_save)
        self.delete_btn = QPushButton("Delete")
        self.delete_btn.clicked.connect(self._on_delete)
        for btn in (self.new_btn, self.dup_btn, self.save_btn, self.delete_btn):
            btn_row.addWidget(btn)
        btn_row.addStretch(1)
        form.addLayout(btn_row)
        self._btn_row = btn_row

        for widget in (self.name_edit, self.desc_edit):
            widget.textChanged.connect(self._mark_dirty)
        self.prompt_edit.textChanged.connect(self._mark_dirty)

    def _header_widget(self):
        """Optional widget shown above the two panes (overridden by subclasses)."""
        return None

    def _mark_dirty(self):
        """Flag unsaved edits and clear any stale status hint."""
        if not self._dirty:
            self.hint_label.setText("")
        self._dirty = True

    def _reload_list(self, select=""):
        """Rebuild the list: built-ins (grayed) first, then user presets. The
        fallback selection stays signal-blocked so no confirm can fire mid-reload."""
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        for name in self.builtin:
            self.list_widget.addItem(f"{name}  (built-in)")
            item = self.list_widget.item(self.list_widget.count() - 1)
            item.setForeground(QColor("#8a8a8a"))
        for name in self.presets.get(self.section, {}):
            self.list_widget.addItem(name)
            if name == select:
                self.list_widget.setCurrentRow(self.list_widget.count() - 1)
        if self.list_widget.currentRow() < 0 and self.list_widget.count():
            self.list_widget.setCurrentRow(0)
        self.list_widget.blockSignals(False)
        self._last_row = self.list_widget.currentRow()
        self._refresh_from_selection()

    def _selected_name(self) -> str:
        """The raw preset name behind the current list item ('' for none)."""
        item = self.list_widget.currentItem()
        if item is None:
            return ""
        return item.text().removesuffix("  (built-in)")

    def _selected_is_builtin(self) -> bool:
        item = self.list_widget.currentItem()
        return item is not None and item.text().endswith("(built-in)")

    def _refresh_from_selection(self):
        """Populate the edit pane from the current list selection (or blank)."""
        name = self._selected_name()
        builtin_mode = self._selected_is_builtin()
        entry = self.builtin.get(name, {}) if builtin_mode else self.presets.get(self.section, {}).get(name, {})
        self.name_edit.setText(name)
        self.desc_edit.setText(entry.get("description", ""))
        self.prompt_edit.setPlainText(entry.get("prompt", ""))
        self._set_pane_editable(not builtin_mode)
        self.save_btn.setEnabled(not builtin_mode and bool(name))
        self.delete_btn.setEnabled(self._is_user(name))
        self.dup_btn.setEnabled(bool(name))
        if builtin_mode:
            self.hint_label.setText("Built-in preset - duplicate to customize.")
            self.hint_label.setStyleSheet("color: #8a8a8a; font-style: italic;")
        else:
            self.hint_label.setText("")
        self.current = "" if builtin_mode else name
        self._dirty = False

    def _on_select(self, _text):
        """Switch entries, confirming before discarding unsaved pane edits."""
        if self._dirty:
            choice = self._confirm_discard()
            if choice == "cancel":
                self.list_widget.blockSignals(True)
                self.list_widget.setCurrentRow(self._last_row)
                self.list_widget.blockSignals(False)
                return
            if choice == "save":
                self._on_save()
                if self._dirty:  # validation failed - keep the pane as-is
                    self._last_row = self.list_widget.currentRow()
                    return
        self._last_row = self.list_widget.currentRow()
        self._refresh_from_selection()

    def _confirm_discard(self) -> str:
        """Ask what to do with unsaved edits; returns 'save' | 'discard' | 'cancel'."""
        box = QMessageBox(self)
        box.setWindowTitle("Unsaved Changes")
        box.setText("The current edit pane has unsaved changes.")
        save_btn = box.addButton("Save", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_btn:
            return "save"
        if box.standardButton(clicked) == QMessageBox.StandardButton.Cancel:
            return "cancel"
        return "discard"

    # --------------------------------------------------------------- actions
    def _resolve_dirty(self) -> bool:
        """Handle unsaved edits before an action; returns False when aborted."""
        if not self._dirty:
            return True
        choice = self._confirm_discard()
        if choice == "cancel":
            return False
        if choice == "save":
            self._on_save()
            if self._dirty:  # validation failed - let the user fix it
                return False
        self._dirty = False
        return True

    def _after_delete(self, name: str):
        """Hook after a preset is removed (overridden by the base prompt editor)."""
        return None

    def _on_new(self):
        """Blank the pane for a fresh user preset (selection-independent)."""
        if not self._resolve_dirty():
            return
        self.list_widget.blockSignals(True)
        self.list_widget.setCurrentRow(-1)
        self.list_widget.blockSignals(False)
        self._last_row = -1
        self._set_pane_editable()
        self.name_edit.clear()
        self.desc_edit.clear()
        self.prompt_edit.clear()
        self.save_btn.setEnabled(True)
        self.delete_btn.setEnabled(False)
        self.dup_btn.setEnabled(False)
        self.hint_label.setText("Fill in the fields and press Save to create the preset.")
        self._dirty = False  # field clears fire textChanged; the fresh pane starts clean
        self.name_edit.setFocus()

    def _on_duplicate(self):
        """Copy the selected entry (built-in or user) into a new editable preset."""
        name = self._selected_name()
        if not name:
            return
        if not self._resolve_dirty():
            return
        builtin_mode = self._selected_is_builtin()
        entry = self.builtin.get(name, {}) if builtin_mode else self.presets.get(self.section, {}).get(name, {})
        base = f"{name} (copy)"
        candidate, n = base, 2
        user_names = self.presets.get(self.section, {})
        while candidate in user_names:
            candidate = f"{base} {n}"
            n += 1
        self.list_widget.blockSignals(True)
        self.list_widget.setCurrentRow(-1)
        self.list_widget.blockSignals(False)
        self._last_row = -1
        self._set_pane_editable()
        self.name_edit.setText(candidate)
        self.desc_edit.setText(entry.get("description", ""))
        self.prompt_edit.setPlainText(entry.get("prompt", ""))
        self.save_btn.setEnabled(True)
        self.delete_btn.setEnabled(False)
        self.dup_btn.setEnabled(False)
        self.hint_label.setText("")
        self._dirty = False  # field sets fire textChanged; the duplicate starts clean
        self.name_edit.setFocus()

    def _unique_name_error(self, name: str) -> str:
        """Validation message for a candidate preset name ('' when acceptable)."""
        if not name.strip():
            return "Name cannot be empty."
        if name.strip() != name:
            return "Name cannot start or end with spaces."
        if name in self.builtin:
            return "That name belongs to a built-in preset - pick another."
        names = self.presets.get(self.section, {})
        if name in names and name != self.current:
            return "A user preset with that name already exists."
        return ""

    def _on_save(self):
        """Validate and persist the pane's fields as a user preset."""
        name = self.name_edit.text().strip()
        prompt = self.prompt_edit.toPlainText().strip()
        error = self._unique_name_error(name)
        if not error and not prompt:
            error = "Prompt cannot be empty."
        if error:
            self.hint_label.setText(error)
            self.hint_label.setStyleSheet("color: #F87171; font-style: italic;")
            return
        self.presets.setdefault(self.section, {})[name] = {
            "prompt": prompt,
            "description": self.desc_edit.text().strip(),
        }
        self.current = name
        prompt_presets.save_presets(self.project_root, self.presets)
        self._reload_list(select=name)
        self.hint_label.setText("Saved to user/prompt_presets.json.")
        self.hint_label.setStyleSheet("color: #4ADE80; font-style: italic;")

    def _on_delete(self):
        """Remove the selected user preset after a confirmation prompt."""
        name = self._selected_name()
        if not self._is_user(name):
            return
        answer = QMessageBox.question(
            self, "Delete Preset", f"Delete user preset '{name}'? Built-in presets stay untouched."
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.presets.get(self.section, {}).pop(name, None)
        self._after_delete(name)
        prompt_presets.save_presets(self.project_root, self.presets)
        self.current = ""
        self._dirty = False  # the pane is about to be replaced; nothing left to save
        self._reload_list()


class CombinationTestTab(QWidget):
    """Preview and generate a rephrased line for a real in-game phrase using an
    exact tone + verbiage combination (the same code path the pipeline runs)."""

    def __init__(self, project_root, presets, tone, wordiness, parent=None):
        super().__init__(parent)
        self.project_root = project_root
        self.presets = presets
        self._config = None
        self._thread = None
        self._entries = self._load_entries()
        self._build_ui()
        self._populate_categories()
        self.tone_combo.setCurrentText(tone)
        self.wordiness_combo.setCurrentText(wordiness)
        self._update_preview()

    # ------------------------------------------------------------------ data
    def _load_entries(self) -> list:
        """Usable transcript entries as dicts: wem_id, transcription, category, intent."""
        try:
            path = self.project_root / "data" / "nms_suit_voice_transcripts.json"
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        entries = []
        for wem_id, entry in raw.items():
            if not isinstance(entry, dict) or entry.get("Used") != "Yes":
                continue
            category = str(entry.get("Category", "")).strip()
            if not category or category.startswith("_"):
                continue
            entries.append({
                "wem_id": str(wem_id),
                "transcription": str(entry.get("Transcription", "")).strip(),
                "category": category,
                "intent": str(entry.get("Intent", "")).strip(),
            })
        return entries

    def _config_shim(self):
        """Lazily build the pipeline config (no LLM) used for prompt assembly."""
        if self._config is None:
            from modular.config import SuitVoiceConfig

            self._config = SuitVoiceConfig(init_llm=False)
            self._config.selection_locked = True
        return self._config

    def _sync_selections(self):
        """Push the dialog's tone/verbiage selection into the config shim."""
        config = self._config_shim()
        config.current_tone = self.tone_combo.currentText()
        config.current_wordiness = self.wordiness_combo.currentText()
        return config

    def _refresh_preset_lists(self):
        """Refresh tone/verbiage combo contents after preset edits in sibling tabs."""
        for combo, section in ((self.tone_combo, "tones"), (self.wordiness_combo, "wordiness")):
            previous = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            builtin_names = sorted(self._builtin_section(section))
            combo.addItems(builtin_names)
            user_names = [n for n in sorted(self.presets.get(section, {})) if n not in builtin_names]
            if user_names:
                combo.insertSeparator(combo.count())
                combo.addItems(user_names)
            if combo.findText(previous) >= 0:
                combo.setCurrentText(previous)
            combo.blockSignals(False)
        self._update_preview()

    def _builtin_section(self, section) -> dict:
        """Built-in names for a section (mirrors the section editors' source)."""
        try:
            path = self.project_root / "data" / "prompt_data.json"
            raw = json.loads(path.read_text(encoding="utf-8"))
            return raw.get(section, {})
        except Exception:
            return {}

    def _sync_config_presets(self):
        """Re-merge the shared preset dict into the config shim after edits."""
        config = self._config_shim()
        try:
            raw = json.loads(
                (self.project_root / "data" / "prompt_data.json").read_text(encoding="utf-8")
            )
        except Exception:
            raw = {}
        merged = prompt_presets.merge_promptdata(raw, self.presets)
        from modular.prompt_utils import build_promptdata

        config.ui_promptdata = merged
        config.promptdata = build_promptdata(merged)
        config.suit_voice_base = prompt_presets.resolve_base(self._builtin_base_text(), self.presets)

    def _builtin_base_text(self) -> str:
        """The pristine built-in base prompt text from data/base_prompt.txt."""
        try:
            return (self.project_root / "data" / "base_prompt.txt").read_text(encoding="utf-8")
        except Exception:
            return ""

    # ------------------------------------------------------------------- ui
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        grid = QGridLayout()
        grid.addWidget(QLabel("Category:"), 0, 0)
        self.category_combo = QComboBox()
        grid.addWidget(self.category_combo, 0, 1)
        grid.addWidget(QLabel("In-game phrase:"), 0, 2)
        self.phrase_combo = QComboBox()
        grid.addWidget(self.phrase_combo, 0, 3)
        grid.addWidget(QLabel("Tone:"), 1, 0)
        self.tone_combo = QComboBox()
        grid.addWidget(self.tone_combo, 1, 1)
        grid.addWidget(QLabel("Verbiage:"), 1, 2)
        self.wordiness_combo = QComboBox()
        grid.addWidget(self.wordiness_combo, 1, 3)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 2)
        root.addLayout(grid)

        root.addWidget(QLabel("Assembled prompt (exactly what the LLM receives):"))
        self.prompt_view = QPlainTextEdit()
        self.prompt_view.setReadOnly(True)
        self.prompt_view.setMaximumHeight(150)
        root.addWidget(self.prompt_view)

        btn_row = QHBoxLayout()
        self.generate_btn = QPushButton("Generate Test Phrase")
        self.generate_btn.setToolTip("Runs the real pipeline prompt through the local LLM (loads it on first use).")
        self.generate_btn.clicked.connect(self._on_generate)
        btn_row.addWidget(self.generate_btn)
        btn_row.addStretch(1)
        root.addLayout(btn_row)

        root.addWidget(QLabel("Generated phrase:"))
        self.output_view = QPlainTextEdit()
        self.output_view.setReadOnly(True)
        self.output_view.setPlaceholderText("Press Generate to rephrase the selected in-game phrase.")
        root.addWidget(self.output_view, 1)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #8a8a8a; font-style: italic;")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        # wiring
        self.category_combo.currentTextChanged.connect(self._on_category_changed)
        self.phrase_combo.currentTextChanged.connect(lambda _t: self._update_preview())
        self.tone_combo.currentTextChanged.connect(lambda _t: self._update_preview())
        self.wordiness_combo.currentTextChanged.connect(lambda _t: self._update_preview())

    def _populate_categories(self):
        """Fill the category combo and the tone/verbiage combos."""
        categories = sorted({e["category"] for e in self._entries})
        self.category_combo.addItems(categories)
        self._refresh_preset_lists()

    def _selected_entry(self) -> dict:
        """The transcript entry matching the current category + phrase selection."""
        category = self.category_combo.currentText()
        phrase = self.phrase_combo.currentText()
        for entry in self._entries:
            if entry["category"] == category and entry["transcription"] == phrase:
                return entry
        return {}

    def _on_category_changed(self, _text):
        """Refill the phrase combo for the selected category, then refresh preview."""
        category = self.category_combo.currentText()
        phrases = sorted({e["transcription"] for e in self._entries if e["category"] == category})
        self.phrase_combo.blockSignals(True)
        self.phrase_combo.clear()
        self.phrase_combo.addItems(phrases)
        self.phrase_combo.blockSignals(False)
        self._update_preview()

    def _update_preview(self):
        """Rebuild the assembled prompt preview from the current selections."""
        entry = self._selected_entry()
        if not entry:
            self.prompt_view.setPlainText("")
            return
        try:
            from modular.prompt_utils import build_suit_prompt

            config = self._sync_selections()
            self._sync_config_presets()
            prompt = build_suit_prompt(
                config, entry["category"], entry["intent"], entry["transcription"], entry["wem_id"]
            )
            self.prompt_view.setPlainText(prompt)
        except Exception as err:
            self.prompt_view.setPlainText(f"Prompt preview unavailable: {err}")

    # ------------------------------------------------------------- generate
    def _on_generate(self):
        """Kick off generation on a worker thread; UI stays responsive."""
        entry = self._selected_entry()
        if not entry:
            self.status_label.setText("Select a category and phrase first.")
            return
        self.generate_btn.setEnabled(False)
        self.status_label.setStyleSheet("color: #8a8a8a; font-style: italic;")
        self.status_label.setText("Preparing...")
        self._thread = FunctionThread(lambda: self._generate_job(entry), self)
        self._thread.log_line.connect(lambda msg: self.status_label.setText(msg))
        self._thread.finished_ok.connect(self._on_generated)
        self._thread.failed.connect(self._on_generate_failed)
        self._thread.start()

    def _generate_job(self, entry: dict) -> str:
        """Worker body: lazy-load the LLM, build the prompt, rephrase. Runs off-GUI."""
        from modular.llm_utils import reword_phrase
        from modular.prompt_utils import build_suit_prompt

        config = self._sync_selections()
        self._sync_config_presets()
        if config.llm is None:
            self.emit_status("Loading local LLM model (first use takes a moment)...")
            config.load_llm()
            self.emit_status("Model loaded - generating...")
        prompt = build_suit_prompt(
            config, entry["category"], entry["intent"], entry["transcription"], entry["wem_id"]
        )
        result = reword_phrase(config, entry["wem_id"], entry["category"], entry["transcription"], prompt)
        return result

    def emit_status(self, message: str):
        """Thread-safe status update from the worker thread."""
        if self._thread is not None:
            self._thread.emit_log(message)

    def _on_generated(self, text):
        self.output_view.setPlainText(text)
        self.status_label.setText("")
        self.generate_btn.setEnabled(True)

    def _on_generate_failed(self, error):
        self.status_label.setStyleSheet("color: #F87171; font-style: italic;")
        self.status_label.setText(f"Generation failed: {error}")
        self.generate_btn.setEnabled(True)

    def release_model(self):
        """Drop the loaded LLM so the manager frees its memory when the dialog closes."""
        if self._config is not None and self._config.llm is not None:
            self._config.llm = None
            import gc

            gc.collect()


class BasePromptEditor(PresetSectionEditor):
    """Base prompt variants: the built-in Default (read-only) plus user rewrites,
    with an Activate control - the active variant shapes every generated line."""

    def __init__(self, project_root, presets, parent=None):
        self._builtin_text = self._read_builtin_text(project_root)
        super().__init__(project_root, prompt_presets.BASE_SECTION, presets, parent=parent)
        self.activate_btn = QPushButton("Activate")
        self.activate_btn.setToolTip("Use this base prompt variant for all generated lines.")
        self.activate_btn.clicked.connect(self._on_activate)
        self._btn_row.insertWidget(0, self.activate_btn)
        self._refresh_from_selection()

    def _header_widget(self):
        """Amber warning: the base prompt shapes every line the suit speaks."""
        warn = QLabel(
            "WARNING: The base prompt defines the suit's core behavior and shapes EVERY line it speaks. "
            "Changes here have a much larger effect than tone or verbiage presets."
        )
        warn.setStyleSheet("color: #FBBF24; font-style: italic;")
        warn.setWordWrap(True)
        return warn

    # ------------------------------------------------------------ overrides
    def _read_builtin_text(self, project_root) -> str:
        """The pristine built-in base prompt from data/base_prompt.txt."""
        try:
            return (Path(project_root) / "data" / "base_prompt.txt").read_text(encoding="utf-8")
        except Exception:
            return ""

    def _load_builtin(self) -> dict:
        """Single built-in entry: the Default base prompt."""
        return {"Default": {"text": self._builtin_text, "description": "Original built-in base prompt."}}

    def _is_active(self, name: str) -> bool:
        """True when this name is the currently active base variant."""
        return self.presets.get(prompt_presets.BASE_ACTIVE_KEY, prompt_presets.ACTIVE_DEFAULT) == name

    def _reload_list(self, select=""):
        """Rebuild the list, marking the active variant with a star."""
        active = self.presets.get(prompt_presets.BASE_ACTIVE_KEY, prompt_presets.ACTIVE_DEFAULT)
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        for name in self.builtin:
            marker = "\u2605 " if name == active else ""
            self.list_widget.addItem(f"{marker}{name}  (built-in)")
            self.list_widget.item(self.list_widget.count() - 1).setForeground(QColor("#8a8a8a"))
        for name in self.presets.get(self.section, {}):
            marker = "\u2605 " if name == active else ""
            self.list_widget.addItem(f"{marker}{name}")
            if name == select:
                self.list_widget.setCurrentRow(self.list_widget.count() - 1)
        if self.list_widget.currentRow() < 0 and self.list_widget.count():
            self.list_widget.setCurrentRow(0)
        self.list_widget.blockSignals(False)
        self._last_row = self.list_widget.currentRow()
        self._refresh_from_selection()

    def _selected_name(self) -> str:
        """Raw preset name behind the current item (strips the star marker)."""
        item = self.list_widget.currentItem()
        if item is None:
            return ""
        return item.text().removeprefix("\u2605 ").removesuffix("  (built-in)")

    def _refresh_from_selection(self):
        """Populate the pane; the text field key is 'text', not 'prompt'."""
        name = self._selected_name()
        builtin_mode = self._selected_is_builtin()
        entry = self.builtin.get(name, {}) if builtin_mode else self.presets.get(self.section, {}).get(name, {})
        self.name_edit.setText(name)
        self.desc_edit.setText(entry.get("description", ""))
        self.prompt_edit.setPlainText(entry.get("text", ""))
        self._set_pane_editable(not builtin_mode)
        self.save_btn.setEnabled(not builtin_mode and bool(name))
        self.delete_btn.setEnabled(self._is_user(name))
        self.dup_btn.setEnabled(bool(name))
        if builtin_mode:
            self.hint_label.setText("Built-in base prompt - duplicate to customize.")
            self.hint_label.setStyleSheet("color: #8a8a8a; font-style: italic;")
        else:
            self.hint_label.setText("")
        self.current = "" if builtin_mode else name
        self._dirty = False

    def _unique_name_error(self, name: str) -> str:
        """Validation message for a candidate base variant name."""
        error = super()._unique_name_error(name)
        if not error and name == prompt_presets.ACTIVE_DEFAULT:
            error = "'Default' is reserved for the built-in base prompt."
        return error

    def _on_save(self):
        """Validate and persist the pane's fields as a base variant. The text is
        stored verbatim; the prompt join normalizes the seam automatically."""
        name = self.name_edit.text().strip()
        text = self.prompt_edit.toPlainText()
        error = self._unique_name_error(name)
        if not error and not text.strip():
            error = "Base prompt text cannot be empty."
        if error:
            self.hint_label.setText(error)
            self.hint_label.setStyleSheet("color: #F87171; font-style: italic;")
            return
        self.presets.setdefault(self.section, {})[name] = {
            "text": text,
            "description": self.desc_edit.text().strip(),
        }
        self.current = name
        prompt_presets.save_presets(self.project_root, self.presets)
        self._reload_list(select=name)
        self.hint_label.setText("Saved to user/prompt_presets.json.")
        self.hint_label.setStyleSheet("color: #4ADE80; font-style: italic;")

    # --------------------------------------------------------------- actions
    def _after_delete(self, name: str):
        """Hook after a preset is removed (overridden by the base prompt editor)."""
        return None

    def _after_delete(self, name: str):
        """Reset to the built-in Default when the active variant is removed."""
        if self.presets.get(prompt_presets.BASE_ACTIVE_KEY) == name:
            self.presets[prompt_presets.BASE_ACTIVE_KEY] = prompt_presets.ACTIVE_DEFAULT

    def _on_activate(self):
        """Make the selected entry (built-in or user variant) the active base prompt."""
        name = self._selected_name()
        if not name:
            return
        self.presets[prompt_presets.BASE_ACTIVE_KEY] = (
            prompt_presets.ACTIVE_DEFAULT if self._selected_is_builtin() else name
        )
        prompt_presets.save_presets(self.project_root, self.presets)
        self._reload_list(select=None if self._selected_is_builtin() else name)
        self.hint_label.setText(f"Active base prompt: {self.presets[prompt_presets.BASE_ACTIVE_KEY]}.")
        self.hint_label.setStyleSheet("color: #4ADE80; font-style: italic;")


class PresetManagerDialog(QDialog):
    """Unified dialog: tone presets, verbiage presets, base prompt and the test."""

    def __init__(self, project_root, tone="", wordiness="", parent=None):
        super().__init__(parent)
        self.project_root = Path(project_root)
        self.presets = prompt_presets.load_presets(self.project_root)

        self.setWindowTitle("Prompt Presets & Combination Test")
        self.resize(760, 560)
        tabs = QTabWidget()
        self.tone_editor = PresetSectionEditor(self.project_root, "tones", self.presets, tone)
        self.wordiness_editor = PresetSectionEditor(self.project_root, "wordiness", self.presets, wordiness)
        self.base_editor = BasePromptEditor(self.project_root, self.presets)
        self.test_tab = CombinationTestTab(self.project_root, self.presets, tone, wordiness)
        tabs.addTab(self.tone_editor, "Tone Presets")
        tabs.addTab(self.wordiness_editor, "Verbiage Presets")
        tabs.addTab(self.base_editor, "Base Prompt")
        tabs.addTab(self.test_tab, "Combination Test")
        tabs.currentChanged.connect(self._on_tab_changed)

        root = QVBoxLayout(self)
        root.addWidget(tabs, 1)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)
        self._tabs = tabs

    def _on_tab_changed(self, index):
        """Keep the test tab current: refresh lists and preview after any edits
        made in the sibling tabs (base activation, preset saves, deletes)."""
        if self._tabs.widget(index) is self.test_tab:
            self.test_tab._refresh_preset_lists()

    def _dirty_editors(self) -> list:
        """Editors with unsaved pane edits."""
        return [e for e in (self.tone_editor, self.wordiness_editor, self.base_editor) if e.is_dirty()]

    def _confirm_close(self) -> bool:
        """Offer to save unsaved edits before closing; False aborts the close."""
        dirty = self._dirty_editors()
        if not dirty:
            return True
        box = QMessageBox(self)
        box.setWindowTitle("Unsaved Changes")
        box.setText("Unsaved preset changes will be lost.")
        save_btn = box.addButton("Save && Close", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Discard Changes", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is cancel_btn:
            return False
        if clicked is save_btn:
            for editor in dirty:
                editor._on_save()
            if self._dirty_editors():  # a validation error blocked saving
                return False
        return True

    def done(self, result):
        """Confirm unsaved edits, then release the test tab's LLM memory."""
        if not self._confirm_close():
            return
        self.test_tab.release_model()
        super().done(result)
