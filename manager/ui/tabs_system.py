# manager/ui/tabs_system.py - System Environment tab: paths, setup, pipeline settings, cache maintenance.
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from manager.setup_runner import SetupRunner, is_setup_complete
from engine import tts_utils
from engine.path_utils import (
    LLM_VARIANTS,
    active_llm_variant,
    is_pipeline_running,
    purge_cache_audio,
    resolve_mod_root,
    resolve_mod_wem_dir,
    unused_llm_variant_file,
)
from ui.style import (
    BEZEL_GAP,
    CONSOLE_COLOR,
    CONSOLE_QSS,
    DIM,
    ERROR,
    GOOD,
    HINT_QSS,
    MONO,
    PAGE_MARGIN,
    WARN,
    make_group,
    status_qss,
)
from ui.tabs_audio import _POCKET_TTS_TERMS_URL, _pocket_model
from ui.workers import FunctionThread

_CLONE_NAME_INVALID = re.compile(r'[\\/:*?"<>|]')


def _sanitize_clone_name(name: str) -> str:
    """Collapse a typed voice name into a filesystem-safe key (spaces become underscores)."""
    cleaned = _CLONE_NAME_INVALID.sub("", name.strip())
    return re.sub(r"\s+", "_", cleaned)


class SystemTab(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self._setup_thread = None
        self._log_offset = 0
        self._clone_thread = None
        self._voice_setup_thread = None
        self._clone_sample_path = None
        self._clone_auth_hint = False
        self._clone_log_tail = []
        self._build_ui()
        self._refresh_clone_list()
        self._sync_setup_button()
        self._pipeline_log_timer = QTimer(self)
        self._pipeline_log_timer.setInterval(1000)
        self._pipeline_log_timer.timeout.connect(self._tail_pipeline_log)
        self._pipeline_log_timer.start()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root_layout = QHBoxLayout(self)
        root_layout.setContentsMargins(PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN)
        root_layout.setSpacing(PAGE_MARGIN)
        # The scroll viewports otherwise paint the raw palette, which renders
        # darker than every other tab; transparency restores the page look.
        self.setStyleSheet(
            "QScrollArea { background: transparent; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
        )

        # Two scrollable columns so nothing clips at small window sizes.
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QFrame.Shape.NoFrame)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        left_host = QWidget()
        left_scroll.setWidget(left_host)
        right_host = QWidget()
        right_scroll.setWidget(right_host)
        left = QVBoxLayout(left_host)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(BEZEL_GAP)
        right = QVBoxLayout(right_host)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(BEZEL_GAP)
        root_layout.addWidget(left_scroll, 5)
        root_layout.addWidget(right_scroll, 4)

        pipeline_group, pipe_layout = make_group(" VOICE PIPELINE ")
        left.addWidget(pipeline_group)

        self.autostart_cb = QCheckBox(
            "Start the pipeline automatically when the utility opens"
        )
        self.autostart_cb.setChecked(
            bool(self.window.env_data.get("runtime", {}).get("autostart_pipeline", True))
        )
        self.autostart_cb.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.autostart_cb.toggled.connect(self._save_autostart)
        self.autostart_cb.setToolTip(
            "Starts the voice engine automatically when this utility opens.\nTurning it off means you press Start in the bottom bar yourself."
        )
        pipe_layout.addWidget(self.autostart_cb)

        self.pipeline_log_btn = QPushButton("Open Pipeline Log")
        self.pipeline_log_btn.clicked.connect(self._open_pipeline_log)
        self.pipeline_log_btn.setToolTip(
            "Opens the raw engine log file (full history). The console below shows the live tail."
        )
        pipe_layout.addWidget(self.pipeline_log_btn)

        paths_group, paths_layout = make_group(" CORE PLATFORM FILE INFRASTRUCTURE ")
        left.addWidget(paths_group)

        paths_layout.addWidget(QLabel("No Man's Sky Target Mod Directory:"))
        dir_label = paths_layout.itemAt(paths_layout.count() - 1).widget()
        dir_label.setWordWrap(True)
        dir_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row = QHBoxLayout()
        stored = (self.window.user_data.get("paths", {}).get("mod_dir")
                  or self.window.env_data.get("paths", {}).get("mod_dir", ""))
        # Display the SIMPLE mod root; the deep ENGLISH(US) watch path is resolved
        # at consumption time by engine.path_utils.
        display = str(resolve_mod_root(stored)) if stored else ""
        self.mod_dir_edit = QLineEdit(display)
        self.mod_dir_edit.setPlaceholderText(
            "C:/Program Files (x86)/Steam/steamapps/common/No Man's Sky/GAMEDATA/MODS/")
        self.mod_dir_edit.textChanged.connect(self._sync_setup_button)
        self.mod_dir_edit.setToolTip(
            "The game's GAMEDATA/MODS folder (Steam or Xbox).\nThe DYNAMIC_SUIT_VOICE sub-path is handled automatically."
        )
        row.addWidget(self.mod_dir_edit)
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_mod_directory)
        row.addWidget(browse_btn)
        paths_layout.addLayout(row)

        # First-time setup -------------------------------------------------
        setup_group, setup_layout = make_group(" FIRST-TIME SETUP ")
        left.addWidget(setup_group)
        self.setup_group = setup_group

        self.setup_btn = QPushButton("Setup")
        self.setup_btn.clicked.connect(self._run_setup)
        self.setup_btn.setToolTip(
            "One-time installation: creates the venv, downloads dependencies,\n"
            "ffmpeg, the wav2wem encoder and the local LLM model(s) (~3 GB total;\n"
            "the E4B variant adds ~4.6 GB when it is selected in Advanced Settings),\n"
            "then copies the starter voice files into your mod directory."
        )
        setup_layout.addWidget(self.setup_btn)

        self.setup_hint = QLabel("Enter a mod directory above to enable Setup.")
        self.setup_hint.setWordWrap(True)
        self.setup_hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setup_hint.setStyleSheet(HINT_QSS)
        setup_layout.addWidget(self.setup_hint)

        self.setup_log = QPlainTextEdit()
        self.setup_log.setReadOnly(True)
        self.setup_log.setMaximumHeight(160)
        self.setup_log.setStyleSheet(CONSOLE_QSS)
        self.setup_log.setVisible(False)  # only shown while Setup actually runs
        setup_layout.addWidget(self.setup_log)

        # Voice cloning ------------------------------------------------------
        clone_group, clone_layout = make_group(" VOICE CLONING ")
        left.addWidget(clone_group)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name:"))
        self.clone_name_edit = QLineEdit()
        self.clone_name_edit.setPlaceholderText("Fairy")
        self.clone_name_edit.setToolTip('Spaces become underscores; \\ / : * ? " < > | are not allowed.')
        self.clone_name_edit.textChanged.connect(self._update_clone_button)
        name_row.addWidget(self.clone_name_edit, 1)
        clone_layout.addLayout(name_row)
        self.clone_name_hint = QLabel("")
        self.clone_name_hint.setStyleSheet(HINT_QSS)
        self.clone_name_hint.setWordWrap(True)
        self.clone_name_hint.setVisible(False)  # collapses the layout line until needed
        clone_layout.addWidget(self.clone_name_hint)

        sample_row = QHBoxLayout()
        self.clone_sample_btn = QPushButton("Choose Sample...")
        self.clone_sample_btn.setToolTip(
            "A clean single-speaker wav, 10-30 seconds, no music or background noise."
        )
        self.clone_sample_btn.clicked.connect(self._choose_clone_sample)
        sample_row.addWidget(self.clone_sample_btn)
        self.clone_sample_label = QLabel("no sample selected")
        self.clone_sample_label.setStyleSheet(f"color: {DIM}")
        self.clone_sample_label.setWordWrap(True)
        self.clone_sample_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        sample_row.addWidget(self.clone_sample_label, 1)
        clone_layout.addLayout(sample_row)

        self.clone_btn = QPushButton("Clone Voice")
        self.clone_btn.setEnabled(False)
        self.clone_btn.setToolTip(
            "Builds a suit voice from the sample. First use downloads the full\n"
            "cloning model once (online); the voice then appears in the Voice\n"
            "Modifier tab's voice dropdown."
        )
        self.clone_btn.clicked.connect(self._trigger_async_clone)
        clone_layout.addWidget(self.clone_btn)

        self.clone_status = QLabel("Clone Queue: Idle")
        self.clone_status.setStyleSheet(f"{MONO} color: {CONSOLE_COLOR};")
        self.clone_status.setWordWrap(True)
        self.clone_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        clone_layout.addWidget(self.clone_status)

        manage_row = QHBoxLayout()
        manage_row.addWidget(QLabel("Manage clone:"))
        self.clone_remove_combo = QComboBox()
        self.clone_remove_combo.setToolTip("Cloned/custom voices; built-ins cannot be managed here.")
        manage_row.addWidget(self.clone_remove_combo, 1)
        self.rename_clone_btn = QPushButton("Rename")
        self.rename_clone_btn.setToolTip(
            "Renames the selected cloned voice; if it is the active voice, the\n"
            "configuration follows the rename.")
        self.rename_clone_btn.clicked.connect(self._rename_custom_voice)
        manage_row.addWidget(self.rename_clone_btn)
        self.remove_clone_btn = QPushButton("Delete")
        self.remove_clone_btn.setToolTip("Deletes the selected cloned voice from user/pocket_voices/.")
        self.remove_clone_btn.clicked.connect(self._remove_custom_voice)
        manage_row.addWidget(self.remove_clone_btn)
        clone_layout.addLayout(manage_row)

        setup_group, setup_layout2 = make_group(" FIRST-TIME CLONING SETUP ")
        self.clone_setup_group = setup_group
        left.addWidget(setup_group)
        setup_hint2 = QLabel("The first clone needs a one-time Hugging Face sign-in and model download.")
        setup_hint2.setWordWrap(True)
        setup_hint2.setStyleSheet(HINT_QSS)
        setup_layout2.addWidget(setup_hint2)
        hf_row = QHBoxLayout()
        self.terms_btn = QPushButton("Open Terms Page")
        self.terms_btn.setToolTip("Accept the model's terms on huggingface.co (free account).")
        self.terms_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(_POCKET_TTS_TERMS_URL)))
        hf_row.addWidget(self.terms_btn)
        self.voice_setup_btn = QPushButton("Sign in && Preload Model")
        self.voice_setup_btn.setToolTip(
            "Opens a Hugging Face sign-in window, saves the token for cloning and\n"
            "pre-downloads the cloning model so Clone Voice never waits on it."
        )
        self.voice_setup_btn.clicked.connect(self._trigger_voice_setup)
        hf_row.addWidget(self.voice_setup_btn)
        setup_layout2.addLayout(hf_row)

        # Troubleshooting ----------------------------------------------------
        maintenance, maint_layout = make_group(" TROUBLESHOOTING ")
        left.addWidget(maintenance)

        self.purge_btn = QPushButton("Wipe Cache and Unused Model")
        self.purge_btn.clicked.connect(self._purge_audio_cache)
        self.purge_btn.setToolTip(
            "Emergency cleanup: deletes all cached voice variants and temp renders, plus the\n"
            "Gemma 4 GGUF of the rephrasing model that is NOT selected (re-downloadable via\n"
            "Setup). The active model and Pocket TTS weights stay untouched. Rarely needed -\n"
            "the mod manages this cache automatically and re-fills it as you play."
        )
        maint_layout.addWidget(self.purge_btn)

        # Live pipeline console: tails cache/pipeline.log
        log_group, log_layout = make_group(" PIPELINE LOG ")
        right.addWidget(log_group, 1)
        self.pipeline_log_view = QPlainTextEdit()
        self.pipeline_log_view.setReadOnly(True)
        self.pipeline_log_view.setMaximumBlockCount(400)
        self.pipeline_log_view.setStyleSheet(CONSOLE_QSS)
        log_layout.addWidget(self.pipeline_log_view)

        # RIGHT PANE: advanced settings ------------------------------------
        vault_group, vault_layout = make_group(" ADVANCED SETTINGS ")
        left.addWidget(vault_group, 1)

        self.warning_label = QLabel(
            "These values trade CPU load, disk use and voice variety against each other.\n"
            "Defaults are recommended; changes land on Apply Changes."
        )
        self.warning_label.setWordWrap(True)
        self.warning_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.warning_label.setStyleSheet(status_qss(ERROR))
        vault_layout.addWidget(self.warning_label)

        runtime = {**self.window.env_data.get("runtime", {}),
                   **self.window.user_data.get("runtime", {})}  # tracked defaults, untracked overrides
        self.cache_max_spin = QSpinBox()
        self.cache_max_spin.setRange(1, 64)
        self.cache_max_spin.setValue(int(runtime.get("quick_cache_max", 16)))
        self.cache_max_spin.setToolTip(
            "How many alternate wordings are kept per active line.\nMore = more variety, more disk + generation time."
        )
        horizon_row = QHBoxLayout()
        horizon_label = QLabel("Variants cached per line:")
        horizon_label.setWordWrap(True)
        horizon_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        # Label takes the row's width (wrapping if needed), input pins right.
        horizon_row.addWidget(horizon_label, 1)
        self.cache_max_spin.setFixedWidth(110)
        horizon_row.addWidget(self.cache_max_spin)
        vault_layout.addLayout(horizon_row)

        self.serve_spin = QSpinBox()
        self.serve_spin.setRange(1, 6)
        self.serve_spin.setValue(max(1, int(runtime.get("serve_workers", 3))))
        self.serve_spin.setToolTip(
            "How many played lines can be delivered to the mod folder in parallel.\n"
            "Takes effect on engine restart."
        )
        serve_row = QHBoxLayout()
        serve_label = QLabel("Serve workers:")
        serve_label.setWordWrap(True)
        serve_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        serve_row.addWidget(serve_label, 1)
        self.serve_spin.setFixedWidth(110)
        serve_row.addWidget(self.serve_spin)
        vault_layout.addLayout(serve_row)

        cpu_count = os.cpu_count() or 4
        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(1, cpu_count)
        self.threads_spin.setValue(max(1, int(runtime.get("gen_threads", min(cpu_count, 8)))))
        self.threads_spin.setToolTip(
            "CPU threads per generation stage (LLM rephrase + TTS render).\n"
            "Lower leaves headroom while gaming; LLM value applies on engine restart."
        )
        threads_row = QHBoxLayout()
        threads_label = QLabel("Generation CPU threads:")
        threads_label.setWordWrap(True)
        threads_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        threads_row.addWidget(threads_label, 1)
        self.threads_spin.setFixedWidth(110)
        threads_row.addWidget(self.threads_spin)
        vault_layout.addLayout(threads_row)

        # Rephrasing model choice: E2B vs E4B GGUF, committed on Apply Changes.
        variant = active_llm_variant(self.window.env_data, self.window.user_data)
        self.model_combo = QComboBox()
        for key, meta in LLM_VARIANTS.items():
            gguf = self.window.project_root / meta["dir"] / meta["file"]
            label = meta["label"] if gguf.is_file() else f"{meta['label']} - missing (Setup fetches)"
            self.model_combo.addItem(label, userData=key)
        index = self.model_combo.findData(variant)
        self.model_combo.setCurrentIndex(index if index >= 0 else 0)
        self.model_combo.setFixedWidth(230)
        self.model_combo.setToolTip(
            "Which Gemma 4 GGUF rephrases the suit's lines.\n"
            "E2B: fast baseline, ~1 s per line, 2.9 GB. E4B: wider phrasing variety, stronger\n"
            "persona flavor, ~3 s per line, 4.6 GB.\n"
            "Switching on Apply Changes restarts the engine (a few seconds to reload).\n"
            "A missing selection falls back to E2B until Setup fetches it."
        )
        model_row = QHBoxLayout()
        model_label = QLabel("Rephrasing model:")
        model_label.setWordWrap(True)
        model_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        model_row.addWidget(model_label, 1)
        model_row.addWidget(self.model_combo)
        vault_layout.addLayout(model_row)
        self.model_combo.currentIndexChanged.connect(self._refresh_setup_panel)
        vault_layout.addStretch(1)

        self._refresh_setup_panel()  # hide the panel when everything is already fine

    # ------------------------------------------------------------ helpers
    def _setup_panel_reason(self) -> str | None:
        """Why the First-Time Setup panel must be visible, or None to hide it."""
        if self.window.setup_pending:
            return "Finish first-time setup to unlock all tabs."
        if not is_setup_complete(self.window.project_root):
            return "Installation incomplete - run Setup to repair it."
        raw = self.mod_dir_edit.text().strip().replace("\\", "/").rstrip("/")
        registered = str(self.window.user_data.get("paths", {}).get("mod_dir", ""))
        registered = registered.replace("\\", "/").rstrip("/")
        if raw and raw != registered:
            return "Directory changed - run Setup to install the starter voice files there."
        meta = LLM_VARIANTS.get(self.model_combo.currentData())
        if meta and not (self.window.project_root / meta["dir"] / meta["file"]).is_file():
            return f"The selected rephrasing model is not downloaded - Setup will fetch {meta['file']}."
        return None

    def _refresh_setup_panel(self):
        """Show the First-Time Setup panel only while something actually needs it."""
        thread = getattr(self, "_setup_thread", None)
        if thread is not None and thread.isRunning():
            return  # never fight a running setup
        reason = self._setup_panel_reason()
        self.setup_group.setVisible(reason is not None)
        if reason and not self.window.setup_pending:
            self.setup_hint.setText(reason)
            self.setup_hint.setStyleSheet(status_qss(WARN))

    def _sync_setup_button(self):
        self.setup_btn.setEnabled(bool(self.mod_dir_edit.text().strip()))
        if self.window.setup_pending:
            if self.setup_btn.isEnabled():
                self.setup_hint.setText(
                    "Ready. Setup installs the venv, dependencies, tools, models and starter voice files."
                )
                self.setup_hint.setStyleSheet(status_qss(GOOD))
            else:
                self.setup_hint.setText("Enter a mod directory above to enable Setup.")
                self.setup_hint.setStyleSheet(HINT_QSS)
        self._refresh_setup_panel()

    def _browse_mod_directory(self):
        chosen = QFileDialog.getExistingDirectory(
            self, "Select No Man's Sky Mod Directory", self.mod_dir_edit.text().strip()
        )
        if chosen:
            self.mod_dir_edit.setText(str(Path(chosen)))

    def _append_log(self, message: str):
        self.setup_log.appendPlainText(message)

    # ----------------------------------------------------- pipeline settings
    def _tail_pipeline_log(self):
        """Append new lines from cache/pipeline.log to the on-screen console."""
        path = self.window.project_root / "state" / "cache" / "pipeline.log"
        try:
            if not path.exists():
                self._log_offset = 0
                return
            size = path.stat().st_size
            if size < self._log_offset:  # log was truncated/rotated
                self._log_offset = 0
                self.pipeline_log_view.clear()
            if size == self._log_offset:
                return
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._log_offset)
                new_text = f.read()
                self._log_offset = f.tell()
            if new_text.strip():
                self.pipeline_log_view.appendPlainText(new_text.rstrip("\n"))
                bar = self.pipeline_log_view.verticalScrollBar()
                bar.setValue(bar.maximum())
        except OSError:
            pass

    def _open_pipeline_log(self):
        log_path = self.window.project_root / "state" / "cache" / "pipeline.log"
        try:
            if not log_path.exists():
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.touch()
            os.startfile(log_path)
        except OSError as exc:
            QMessageBox.warning(self, "Log Error", f"Could not open the pipeline log:\n{exc}")

    def _save_autostart(self, checked: bool):
        self.window.user_data.setdefault("runtime", {})["autostart_pipeline"] = bool(checked)
        try:
            self.window.user_config_file.write_text(
                json.dumps(self.window.user_data, indent=4), encoding="utf-8"
            )
        except OSError:
            pass

    def _set_status(self, text: str, color: str):
        """Flash a transient message in the main status bar (bottom strip)."""
        bar = getattr(self.window, "pipeline_bar", None)
        if bar is not None:
            bar.flash_message(f"Status: {text}", color, 5)

    # -------------------------------------------------------------- setup
    def _run_setup(self):
        raw_dir = self.mod_dir_edit.text().strip()
        if not raw_dir:
            return
        target = Path(raw_dir)
        if not target.exists():
            if QMessageBox.question(
                self, "Create Directory",
                f"Directory does not exist:\n{target}\n\nCreate it?",
            ) != QMessageBox.StandardButton.Yes:
                return
        # Install into the game's nested mod sub-path (what the pipeline watches
        # and what is_setup_complete checks), not the flat root the user typed.
        mod_wem_dir = resolve_mod_wem_dir(target)
        if QMessageBox.question(
            self, "Confirm Setup",
            f"Voice files will be installed into:\n{mod_wem_dir}\n\n"
            "This also creates the venv, downloads dependencies, ffmpeg, the wav2wem\n"
            "encoder and the local LLM model(s) (approx 3 GB; the E4B variant adds\n"
            "~4.6 GB when selected in Advanced Settings). Continue?",
        ) != QMessageBox.StandardButton.Yes:
            return

        self.setup_btn.setEnabled(False)
        self.mod_dir_edit.setEnabled(False)
        self.setup_hint.setText("Setup running - see log below. Do not close the utility.")
        self.setup_hint.setStyleSheet(status_qss(WARN))
        self.setup_log.clear()
        self.setup_log.setVisible(True)

        runner = SetupRunner(self.window.project_root, mod_wem_dir, log=self._append_log)
        self._setup_thread = _start_thread(self, runner.run, self._setup_finished)
        # Route the runner's log calls through the thread signal: the runner
        # executes on a worker thread and must never touch the log widget.
        runner.log = lambda msg: self._setup_thread.log_line.emit(str(msg))

    def _setup_finished(self, ok: bool, error: str | None):
        self.setup_btn.setEnabled(True)
        self.mod_dir_edit.setEnabled(True)
        if ok:
            self.setup_log.setVisible(False)  # reclaim the space on success
        self._sync_setup_button()
        if ok:
            if self.window.setup_pending:
                self.setup_hint.setText("Setup complete. Restart the utility to unlock all tabs.")
                self.setup_hint.setStyleSheet(status_qss(GOOD))
                QMessageBox.information(
                    self, "Setup Complete",
                    "Setup finished successfully!\n\nRestart the Management Utility to unlock all tabs.",
                )
            else:
                self._set_status("Setup complete", GOOD)
        else:
            self.setup_hint.setText("Setup failed - see the log for details.")
            self.setup_hint.setStyleSheet(status_qss(ERROR))
            QMessageBox.critical(self, "Setup Failed", f"Setup encountered an issue:\n{error}")

    # -------------------------------------------------------------- voice cloning
    def _cloning_ready(self) -> bool:
        """True when cloning can proceed without the setup button: the gated
        cloning model is cached locally, or a Hugging Face credential exists."""
        if os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN"):
            return True
        if (Path.home() / ".cache" / "huggingface" / "token").is_file():
            return True
        if (self.window.project_root / "state" / "cache" / "token").is_file():
            return True
        model_key = _pocket_model(self.window)
        hub = self.window.project_root / "state" / "cache" / "hub"
        return any(
            hub.glob(f"models--kyutai--pocket-tts/snapshots/*/languages/{model_key}/*.safetensors"))

    def _update_clone_setup_visibility(self):
        """The first-time cloning setup group only shows when cloning is not ready yet."""
        self.clone_setup_group.setVisible(not self._cloning_ready())

    def _refresh_clone_list(self):
        """Fill the remove-clone dropdown with cloned/custom voices (built-ins excluded)."""
        self.clone_remove_combo.blockSignals(True)
        self.clone_remove_combo.clear()
        model_name = _pocket_model(self.window)
        for voice in tts_utils.list_pocket_voices(self.window.project_root, model_name):
            if voice not in tts_utils.PREDEFINED_POCKET_VOICES:
                self.clone_remove_combo.addItem(voice, voice)
        self.clone_remove_combo.blockSignals(False)
        self.remove_clone_btn.setEnabled(self.clone_remove_combo.count() > 0)
        self.rename_clone_btn.setEnabled(self.clone_remove_combo.count() > 0)
        self._update_clone_setup_visibility()

    def _choose_clone_sample(self):
        start = self.window.last_browse_dir("clone_sample")
        path, _ = QFileDialog.getOpenFileName(self, "Choose a Voice Sample", start, "WAV Audio (*.wav)")
        if path:
            self.window.remember_browse_dir("clone_sample", path)
            self._clone_sample_path = Path(path)
            self.clone_sample_label.setText(self._clone_sample_path.name)
            self._update_clone_button()

    def _update_clone_button(self):
        name = self.clone_name_edit.text().strip()
        invalid = bool(_CLONE_NAME_INVALID.search(name))
        valid = bool(name) and not invalid
        busy = self._clone_thread is not None
        self.clone_btn.setEnabled(valid and self._clone_sample_path is not None and not busy)
        if invalid:
            self.clone_name_hint.setStyleSheet(status_qss(WARN))
            self.clone_name_hint.setText('That name contains a character Windows paths cannot use: \\ / : * ? " < > |')
        elif valid and _sanitize_clone_name(name) != name:
            self.clone_name_hint.setStyleSheet(HINT_QSS)
            self.clone_name_hint.setText(f"Will be cloned as: {_sanitize_clone_name(name)}")
        else:
            self.clone_name_hint.setText("")
        # An empty label still reserves its layout line - collapse it entirely.
        self.clone_name_hint.setVisible(bool(self.clone_name_hint.text()))

    def _trigger_async_clone(self):
        name = _sanitize_clone_name(self.clone_name_edit.text())
        sample = self._clone_sample_path
        if not name or not sample:
            return
        if name in tts_utils.PREDEFINED_POCKET_VOICES:
            QMessageBox.critical(self, "Clone Error", f"'{name}' is a built-in voice; choose another name.")
            return
        model = _pocket_model(self.window)
        dest = self.window.project_root / "state" / "user" / "pocket_voices" / name / "_model_states" / f"{model}.safetensors"
        if dest.exists():
            QMessageBox.critical(self, "Clone Error", f"A clone named '{name}' already exists for model {model}.")
            return
        exe = Path(sys.executable).parent / "pocket-tts.exe"
        if not exe.is_file():
            QMessageBox.critical(self, "Clone Error", f"pocket-tts.exe not found next to {sys.executable}.")
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._clone_auth_hint = False
        self._clone_log_tail = []
        self.clone_btn.setEnabled(False)
        self.clone_status.setText("Clone Queue: Preparing clone...")
        cmd = [str(exe), "export-voice", str(sample), str(dest), "--language", model]
        self._clone_thread = FunctionThread(lambda: self._clone_job(cmd, dest), self)
        self._clone_thread.log_line.connect(self._on_clone_log)
        self._clone_thread.finished_ok.connect(lambda _r: self._clone_finished(None, name))
        self._clone_thread.failed.connect(lambda err: self._clone_finished(err, name))
        self._clone_thread.start()

    def _clone_job(self, cmd, dest):
        create_no_window = 0x08000000 if sys.platform == "win32" else 0
        # Scrub the runtime-offline flags: cloning needs the full gated model,
        # which may not be in the redirected HF cache yet (one-time download).
        env = dict(os.environ)
        for flag in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
            env.pop(flag, None)
        tts_utils._ensure_pocket_cache_env(self.window.project_root)
        env.setdefault("HF_HOME", str(self.window.project_root / "state" / "cache"))
        hub_dir = self.window.project_root / "state" / "cache" / "hub"
        env.setdefault("HF_HUB_CACHE", str(hub_dir))
        env.setdefault("HUGGINGFACE_HUB_CACHE", str(hub_dir))
        if not env.get("HF_TOKEN"):
            token_file = Path.home() / ".cache" / "huggingface" / "token"
            if token_file.is_file():
                env["HF_TOKEN"] = token_file.read_text(encoding="utf-8").strip()
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", creationflags=create_no_window, env=env,
        )
        for line in proc.stdout:
            self._clone_thread.emit_log(line.rstrip())
        proc.wait()
        if proc.returncode != 0 or not dest.is_file():
            raise RuntimeError(f"export-voice exited with code {proc.returncode}")

    def _on_clone_log(self, line):
        """GUI-side clone log: classify the phase for the status label and keep
        a short tail for the failure dialog."""
        self._clone_log_tail.append(line)
        self._clone_log_tail = self._clone_log_tail[-6:]
        lowered = line.lower()
        if "%" in line and ("it/s" in lowered or "b/s" in lowered):
            phase = "Downloading cloning model (one-time)..."
        elif "loading ttsmodel weights" in lowered or "loading tokenizers" in lowered:
            phase = "Loading the cloning model..."
        elif "encoding audio prompt" in lowered:
            phase = "Analyzing the sample voice..."
        elif "prompting audio" in lowered or "size of the model state" in lowered:
            phase = "Writing the voice state..."
        else:
            return
        self.clone_status.setText(f"Clone Queue: {phase}")
        if "voice cloning" in lowered:
            self._clone_auth_hint = True

    def _clone_finished(self, error, name):
        self._clone_thread = None
        self._update_clone_button()
        if error is None:
            self.clone_status.setText("Clone Queue: Voice ready.")
            self._refresh_clone_list()
            self.window.refresh_voices()
            QMessageBox.information(
                self, "Success",
                f"Cloned voice '{name}' is ready.\n\n"
                "Select it in the Voice Modifier tab and press Apply Changes\n"
                "to make it your suit's voice.",
            )
        else:
            self.clone_status.setText("Clone Queue: Failed.")
            tail = "\n".join(self._clone_log_tail[-4:])
            hint = ""
            if self._clone_auth_hint:
                self.clone_setup_group.setVisible(True)  # one click from fixed
                hint = ("\n\nPress 'Sign in & Preload Model' in System Environment\n"
                        "(one-time Hugging Face sign-in), then retry.")
            QMessageBox.critical(
                self, "Clone Failed",
                f"Cloning '{name}' failed:\n{error}\n\nLast log lines:\n{tail}{hint}",
            )

    def _voice_setup_emit(self, message: str):
        """Thread-safe phase text into the clone status label."""
        if self._voice_setup_thread is not None:
            self._voice_setup_thread.emit_log(str(message)[:160])

    def _trigger_voice_setup(self):
        """One-time Hugging Face sign-in + cloning-model preload."""
        if self._voice_setup_thread is not None:
            return
        self.voice_setup_btn.setEnabled(False)
        self.clone_status.setText("Setup: opening the Hugging Face sign-in window...")
        self._voice_setup_thread = FunctionThread(self._voice_setup_job, self)
        self._voice_setup_thread.log_line.connect(self.clone_status.setText)
        self._voice_setup_thread.finished_ok.connect(lambda _r: self._voice_setup_finished(None))
        self._voice_setup_thread.failed.connect(self._voice_setup_finished)
        self._voice_setup_thread.start()

    def _voice_setup_job(self):
        py_exe = Path(sys.executable).with_name("python.exe")
        if not py_exe.is_file():
            py_exe = Path(sys.executable)
        # 1. Interactive login in its own console (the manager runs windowless).
        self._voice_setup_emit("A sign-in window has opened - paste your Hugging Face access token there.")
        proc = subprocess.Popen(
            [str(py_exe), "-m", "huggingface_hub.commands.huggingface_cli", "login"],
            cwd=str(self.window.project_root),
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        rc = proc.wait()
        if rc != 0:
            self._voice_setup_emit(f"Sign-in window exited with code {rc} (you can retry).")
        # 2. Bridge the token into the project HF cache (HF_HOME-derived token path).
        sys_tok = Path.home() / ".cache" / "huggingface" / "token"
        loc_tok = self.window.project_root / "state" / "cache" / "token"
        if sys_tok.is_file() and not loc_tok.exists():
            loc_tok.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sys_tok, loc_tok)
        self._voice_setup_emit("Token saved for cloning.")
        # 3. Preload the pinned cloning model into the project cache (streams progress).
        model_key = _pocket_model(self.window)
        root = self.window.project_root
        code = (
            "import os; from pathlib import Path; "
            f"root = Path({str(root)!r}); p = root / 'state' / 'cache'; "
            "os.environ['HF_HOME'] = str(p); "
            "os.environ['HF_HUB_CACHE'] = str(p / 'hub'); "
            "os.environ['HUGGINGFACE_HUB_CACHE'] = str(p / 'hub'); "
            "from pocket_tts import TTSModel; "
            f"TTSModel.load_model(language={model_key!r}); print('Cloning model ready.')"
        )
        env = dict(os.environ)
        for flag in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
            env.pop(flag, None)
        tts_utils._ensure_pocket_cache_env(root)
        if not env.get("HF_TOKEN") and sys_tok.is_file():
            env["HF_TOKEN"] = sys_tok.read_text(encoding="utf-8").strip()
        proc = subprocess.Popen(
            [str(py_exe), "-c", code],
            cwd=str(root), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=0x08000000 if sys.platform == "win32" else 0,
        )
        for line in proc.stdout:
            if line.strip():
                self._voice_setup_emit(line.rstrip())
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"Model preload failed (exit code {proc.returncode})")

    def _voice_setup_finished(self, error):
        self._voice_setup_thread = None
        self.voice_setup_btn.setEnabled(True)
        if error is None:
            self.clone_status.setText("Setup Queue: Sign-in and model preload complete.")
            QMessageBox.information(
                self, "Success",
                "Hugging Face sign-in saved and the cloning model is preloaded.\n"
                "Clone Voice is ready to run fully offline from here on.")
            self._refresh_clone_list()
            self.window.refresh_voices()
        else:
            self.clone_status.setText("Setup Queue: Failed.")
            QMessageBox.critical(self, "Setup Failed", f"Voice cloning setup failed:\n{error}")

    def _rename_custom_voice(self):
        """Rename a cloned voice folder; the active voice (config + active rack) follows."""
        old = self.clone_remove_combo.currentData()
        if not old:
            return
        new, ok = QInputDialog.getText(self, "Rename Clone", "New name:", text=old)
        if not ok:
            return
        new = _sanitize_clone_name(new)
        if not new or new == old:
            return
        voices_root = Path(self.window.project_root) / "state" / "user" / "pocket_voices"
        dest = voices_root / new
        if dest.exists():
            QMessageBox.critical(self, "Rename Clone", f"A voice named '{new}' already exists.")
            return
        try:
            (voices_root / old).rename(dest)
        except OSError as e:
            QMessageBox.critical(self, "Rename Clone", f"Could not rename the voice folder:\n{e}")
            return
        if self.window.user_data.get("tts_voice") == old:
            self.window.user_data["tts_voice"] = new
            try:
                self.window.user_config_file.write_text(
                    json.dumps(self.window.user_data, indent=4), encoding="utf-8"
                )
            except OSError:
                pass
        active_file = self.window.project_root / "state" / "user" / "active_eqfx.json"
        if active_file.exists():
            try:
                data = json.loads(active_file.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("tts_voice") == old:
                    data["tts_voice"] = new
                    active_file.write_text(json.dumps(data, indent=4), encoding="utf-8")
            except (OSError, ValueError):
                pass
        self._refresh_clone_list()
        self.window.refresh_voices()
        self.clone_status.setText(f"Clone Queue: '{old}' renamed to '{new}'.")

    def _remove_custom_voice(self):
        key = self.clone_remove_combo.currentData()
        if not key:
            return
        if QMessageBox.question(
            self, "Confirm Deletion",
            f"Permanently delete the cloned voice '{key}' from\n"
            f"state/user/pocket_voices/?\n\nThis cannot be undone.",
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            voice_folder = Path(self.window.project_root) / "state" / "user" / "pocket_voices" / key
            if voice_folder.exists():
                shutil.rmtree(voice_folder)
            self._refresh_clone_list()
            self.window.refresh_voices()
            QMessageBox.information(self, "Success", "Cloned voice removed cleanly.")
        except Exception as e:
            QMessageBox.critical(self, "Purge Failure", f"Could not remove voice folder modules: {e}")

    # -------------------------------------------------------------- purge
    def _purge_audio_cache(self):
        if is_pipeline_running():
            if QMessageBox.question(
                self, "Pipeline Running",
                "The suit voice pipeline appears to be running.\n\n"
                "Purging now is safe but the pipeline may regenerate variants mid-purge.\n"
                "For a clean slate after changing settings: quit the pipeline first.\n\nPurge anyway?",
            ) != QMessageBox.StandardButton.Yes:
                return
        if QMessageBox.question(
            self, "Confirm Purge",
            "Permanently erase all pre-generated voice variants and temporary\n"
            "render files from the local cache folders?\n\n"
            "This also deletes the Gemma 4 GGUF of the rephrasing model that is\n"
            "NOT selected (re-download it any time via Setup).\n\n"
            "(The active model and Pocket TTS weights are NOT touched.)",
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            purged = purge_cache_audio(self.window.project_root, self.window.env_data)
            removed_model = ""
            unused_gguf = unused_llm_variant_file(
                self.window.project_root, self.window.env_data, self.window.user_data)
            if unused_gguf.is_file():
                unused_gguf.unlink()
                removed_model = f"\n\nUnused model file removed: {unused_gguf.name}"
            QMessageBox.information(
                self, "Success",
                f"Exosuit local audio storage purged cleanly ({purged} files)!{removed_model}")
        except Exception as err:
            QMessageBox.critical(self, "Purge Error", f"Could not flush file boundaries: {err}")


def _start_thread(tab, fn, finished_handler):
    """Runs fn on a FunctionThread; routes log_line/finished_ok/failed to the tab."""
    from ui.workers import FunctionThread

    thread = FunctionThread(fn, tab)

    def on_ok(_result):
        finished_handler(True, None)

    def on_fail(error):
        finished_handler(False, error)

    thread.log_line.connect(tab._append_log)
    thread.finished_ok.connect(on_ok)
    thread.failed.connect(on_fail)
    tab._threads = getattr(tab, "_threads", [])
    tab._threads.append(thread)
    thread.start()
    return thread
