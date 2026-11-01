# manager/ui/tabs_system.py - System Environment tab: paths, setup, pipeline settings, cache maintenance.
import json
import os
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from manager.setup_runner import SetupRunner
from modular.path_utils import is_pipeline_running, purge_cache_audio, resolve_mod_root


class SystemTab(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self._setup_thread = None
        self._topup_thread = None
        self._log_offset = 0
        self._build_ui()
        self._sync_setup_button()
        self._pipeline_log_timer = QTimer(self)
        self._pipeline_log_timer.setInterval(1000)
        self._pipeline_log_timer.timeout.connect(self._tail_pipeline_log)
        self._pipeline_log_timer.start()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root_layout = QHBoxLayout(self)
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
        left.setContentsMargins(0, 0, 4, 0)
        right = QVBoxLayout(right_host)
        right.setContentsMargins(4, 0, 0, 0)
        root_layout.addWidget(left_scroll, 5)
        root_layout.addWidget(right_scroll, 6)

        pipeline_group = QGroupBox(" Voice Pipeline ")
        left.addWidget(pipeline_group)
        pipe_layout = QVBoxLayout(pipeline_group)

        self.autostart_cb = QCheckBox(
            "Start the pipeline automatically when the utility opens"
        )
        self.autostart_cb.setChecked(
            bool(self.window.env_data.get("runtime", {}).get("autostart_pipeline", True))
        )
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

        pipe_hint = QLabel(
            "Closing this window keeps the engine running in the tray. "
            "Use the tray icon's Exit to stop the engine and quit."
        )
        pipe_hint.setStyleSheet("color: #8a8a8a; font-style: italic;")
        pipe_hint.setWordWrap(True)
        pipe_layout.addWidget(pipe_hint)

        paths_group = QGroupBox(" Core Platform File Infrastructure ")
        right.addWidget(paths_group)
        paths_layout = QVBoxLayout(paths_group)

        paths_layout.addWidget(QLabel("No Man's Sky Target Mod Directory:"))
        row = QHBoxLayout()
        stored = (self.window.user_data.get("paths", {}).get("mod_dir")
                  or self.window.env_data.get("paths", {}).get("mod_dir", ""))
        # Display the SIMPLE mod root; the deep ENGLISH(US) watch path is resolved
        # at consumption time by modular.path_utils.
        display = str(resolve_mod_root(stored)) if stored else ""
        self.mod_dir_edit = QLineEdit(display)
        self.mod_dir_edit.textChanged.connect(self._sync_setup_button)
        self.mod_dir_edit.setToolTip(
            "The game's GAMEDATA/MODS folder (Steam or Xbox).\nThe DYNAMIC_SUIT_VOICE sub-path is handled automatically."
        )
        row.addWidget(self.mod_dir_edit)
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_mod_directory)
        row.addWidget(browse_btn)
        paths_layout.addLayout(row)
        hint = QLabel(
            "\u21b3 Example: C:/Program Files (x86)/Steam/steamapps/common/No Man's Sky/GAMEDATA/MODS/"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8a8a8a; font-style: italic;")
        paths_layout.addWidget(hint)

        # First-time setup -------------------------------------------------
        setup_group = QGroupBox(" First-Time Setup ")
        right.addWidget(setup_group)
        setup_layout = QVBoxLayout(setup_group)

        self.setup_btn = QPushButton("Setup")
        self.setup_btn.clicked.connect(self._run_setup)
        self.setup_btn.setToolTip(
            "One-time installation: creates the venv, downloads dependencies,\n"
            "ffmpeg, the wav2wem encoder and the local LLM (~3 GB total), then\n"
            "copies the starter voice files into your mod directory."
        )
        setup_layout.addWidget(self.setup_btn)

        self.setup_hint = QLabel("Enter a mod directory above to enable Setup.")
        self.setup_hint.setWordWrap(True)
        self.setup_hint.setStyleSheet("color: #8a8a8a; font-style: italic;")
        setup_layout.addWidget(self.setup_hint)

        self.setup_log = QPlainTextEdit()
        self.setup_log.setReadOnly(True)
        self.setup_log.setMaximumHeight(160)
        self.setup_log.setStyleSheet("background-color: #121212; color: #00FF00; font-family: Consolas;")
        self.setup_log.setVisible(False)  # only shown while Setup actually runs
        setup_layout.addWidget(self.setup_log)

        # Maintenance ------------------------------------------------------
        maintenance = QGroupBox(" Exosuit Core Maintenance Deck ")
        left.addWidget(maintenance)
        maint_layout = QVBoxLayout(maintenance)

        # Maintenance buttons stack vertically: the row would clip at narrow
        # window widths (pushbuttons don't wrap or elide their labels).
        self.topup_btn = QPushButton("Execute Cache Top-Up Sequence")
        self.topup_btn.clicked.connect(self._run_async_topup)
        self.topup_btn.setToolTip(
            "Optional: pre-generates rephrased voice variants for every active line\n"
            "so repeated lines vary instantly in-game. Slow - the pipeline also\n"
            "fills this automatically while you play."
        )
        maint_layout.addWidget(self.topup_btn)
        self.purge_btn = QPushButton("Wipe Local Cached Wave Streams")
        self.purge_btn.clicked.connect(self._purge_audio_cache)
        self.purge_btn.setToolTip(
            "Emergency cleanup: deletes cached voice variants and temp renders\n"
            "(model weights untouched). Normally unnecessary - the mod manages\n"
            "this cache automatically."
        )
        maint_layout.addWidget(self.purge_btn)

        self.status_label = QLabel("Deck Systems Status: Idle")
        self.status_label.setStyleSheet("font-weight: bold; color: #4ADE80;")
        maint_layout.addWidget(self.status_label)

        # Live pipeline console: tails cache/pipeline.log
        log_group = QGroupBox(" Pipeline Log ")
        left.addWidget(log_group, 1)
        log_layout = QVBoxLayout(log_group)
        self.pipeline_log_view = QPlainTextEdit()
        self.pipeline_log_view.setReadOnly(True)
        self.pipeline_log_view.setMaximumBlockCount(400)
        self.pipeline_log_view.setStyleSheet(
            "background-color: #121212; color: #9CDCFE; font-family: Consolas;"
        )
        log_layout.addWidget(self.pipeline_log_view)

        # RIGHT PANE: hot stove burner vault --------------------------------
        vault_group = QGroupBox(" Advanced Signal Diagnostics (Hot Stove Burner Vault) ")
        right.addWidget(vault_group, 1)
        vault_layout = QVBoxLayout(vault_group)

        self.warning_text = (
            "YOU TRULY DO NOT WANT TO DO THIS. But you cannot help but touch the hot stove burner.\n\n"
            "Very well. To open this panel, acknowledge that YOU UNDERSTAND CHANGING THESE SETTINGS COULD CAUSE "
            "UNINTENDED EFFECTS such as: complete mod failure, the total inability for your suit AI to tell Friend "
            "from Foe, severe semantic hallucinations from your local neural hulls, your domestic cat refusing to "
            "recognize you and aggressively hissing at your presence, every single left sock you own for the rest "
            "of your days vanishing from reality, and your tongue turning a very vivid (although highly "
            "interesting) shade of lavender.\n\n"
            "YOU TAKE FULL RESPONSIBILITY if your operating system autonomously purchases a heavy industrial "
            "snowblower from an online auction site and prowls the neighbourhood to chase teenagers celebrating "
            "their graduation."
        )
        self.halloween_easter_egg = (
            "\n\n----------------------------------------------------------\n"
            "\u26a0\ufe0fCRITICAL WARNING:\n CONSIDER YOUR LIFE CHOICES WHILE YOU STILL CAN.\n\n"
            "When the Cenobites arrive, they will not listen to your pleas of \n\"I did not know what would happen.\"\n\n"
            "You clicked the box.\n\nThey will come.\n\nThey have such configurations to show you..."
        )
        self._vault_brief = (
            "These settings can break the mod or make your suit say very odd things.\n"
            "Changing them is entirely at your own risk - hover the checkbox for details."
        )
        self.halloween_easter_egg = (
            "\n\n----------------------------------------------------------\n"
            "\u26a0\ufe0fCRITICAL WARNING:\n CONSIDER YOUR LIFE CHOICES WHILE YOU STILL CAN.\n\n"
            "When the Cenobites arrive, they will not listen to your pleas of \n\"I did not know what would happen.\"\n\n"
            "You clicked the box.\n\nThey will come.\n\nThey have such configurations to show you..."
        )
        self.warning_label = QLabel(self._vault_brief)
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("color: #F87171; font-style: italic;")
        vault_layout.addWidget(self.warning_label)

        self.accept_responsibility = QCheckBox("I ACCEPT ALL ULTIMATE RESPONSIBILITY AND TOUCH THE BURNER")
        self.accept_responsibility.toggled.connect(self._toggle_stove_burner_lock)
        lore = (
            self.warning_text.replace("&", "&amp;").replace("<", "&lt;")
            .replace("\n\n", "</p><p>").replace("\n", " ")
        )
        self.accept_responsibility.setToolTip(f"<div style='width: 360px'><p>{lore}</p></div>")
        vault_layout.addWidget(self.accept_responsibility)

        runtime = self.window.env_data.get("runtime", {})
        self.cache_max_spin = QSpinBox()
        self.cache_max_spin.setRange(1, 64)
        self.cache_max_spin.setValue(int(runtime.get("quick_cache_max", 16)))
        self.cache_max_spin.setEnabled(False)
        self.cache_max_spin.setToolTip(
            "How many alternate wordings are kept per active line.\nMore = more variety, more disk + generation time."
        )
        vault_layout.addWidget(QLabel("Exosuit Pre-Cache Max Threshold Horizon:"))
        vault_layout.addWidget(self.cache_max_spin)

        self.interval_spin = QDoubleSpinBox()
        self.interval_spin.setRange(0.01, 5.0)
        self.interval_spin.setDecimals(2)
        self.interval_spin.setSingleStep(0.05)
        self.interval_spin.setValue(float(runtime.get("check_interval", 0.1)))
        self.interval_spin.setEnabled(False)
        self.interval_spin.setToolTip(
            "How often the pipeline scans the game folder for newly played lines.\nLeave this alone unless lines feel delayed."
        )
        vault_layout.addWidget(QLabel("Filesystem Scan Pool Tick Interval (Seconds):"))
        vault_layout.addWidget(self.interval_spin)
        vault_layout.addStretch(1)

    # ------------------------------------------------------------ helpers
    def _sync_setup_button(self):
        if self.mod_dir_edit.text().strip():
            self.setup_btn.setEnabled(True)
            self.setup_hint.setText(
                "Ready. Setup installs the venv, dependencies, tools, models and starter voice files."
            )
            self.setup_hint.setStyleSheet("color: #4ADE80; font-style: italic;")
        else:
            self.setup_btn.setEnabled(False)
            self.setup_hint.setText("Enter a mod directory above to enable Setup.")
            self.setup_hint.setStyleSheet("color: #8a8a8a; font-style: italic;")

    def _browse_mod_directory(self):
        chosen = QFileDialog.getExistingDirectory(
            self, "Select No Man's Sky Mod Directory", self.mod_dir_edit.text().strip()
        )
        if chosen:
            self.mod_dir_edit.setText(str(Path(chosen)))

    def _append_log(self, message: str):
        self.setup_log.appendPlainText(message)

    def _append_topup_log(self, message: str):
        self.pipeline_log_view.appendPlainText(message)

    # ----------------------------------------------------- pipeline settings
    def _tail_pipeline_log(self):
        """Append new lines from cache/pipeline.log to the on-screen console."""
        path = self.window.project_root / "cache" / "pipeline.log"
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
        log_path = self.window.project_root / "cache" / "pipeline.log"
        try:
            if not log_path.exists():
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.touch()
            os.startfile(log_path)
        except OSError as exc:
            QMessageBox.warning(self, "Log Error", f"Could not open the pipeline log:\n{exc}")

    def _save_autostart(self, checked: bool):
        self.window.env_data.setdefault("runtime", {})["autostart_pipeline"] = bool(checked)
        try:
            self.window.env_config_file.write_text(
                json.dumps(self.window.env_data, indent=4), encoding="utf-8"
            )
        except OSError:
            pass

    def _set_status(self, text: str, color: str):
        self.status_label.setText(f"Deck Systems Status: {text}")
        self.status_label.setStyleSheet(f"font-weight: bold; color: {color};")

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
        mod_wem_dir = resolve_mod_root(target)
        if QMessageBox.question(
            self, "Confirm Setup",
            f"Voice files will be installed into:\n{mod_wem_dir}\n\n"
            "This also creates the venv, downloads dependencies, ffmpeg, the wav2wem\n"
            "encoder and the local LLM model (approx 3 GB total). Continue?",
        ) != QMessageBox.StandardButton.Yes:
            return

        self.setup_btn.setEnabled(False)
        self.mod_dir_edit.setEnabled(False)
        self.setup_hint.setText("Setup running - see log below. Do not close the utility.")
        self.setup_hint.setStyleSheet("color: #FBBF24; font-style: italic;")
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
        self._sync_setup_button()
        if ok:
            self.setup_log.setVisible(False)  # reclaim the space on success
            self.setup_hint.setText("Setup complete. Restart the utility to unlock all tabs.")
            self.setup_hint.setStyleSheet("color: #4ADE80; font-style: italic;")
            QMessageBox.information(
                self, "Setup Complete",
                "Setup finished successfully!\n\nRestart the Management Utility to unlock all tabs.",
            )
        else:
            self.setup_hint.setText("Setup failed - see the log for details.")
            self.setup_hint.setStyleSheet("color: #F87171; font-style: italic;")
            QMessageBox.critical(self, "Setup Failed", f"Setup encountered an issue:\n{error}")

    # ------------------------------------------------------------- top-up
    def _run_async_topup(self):
        # Warn up front: a full top-up renders used-lines x variants and takes
        # hours on CPU. It is incremental and safe to stop at any point.
        from modular.config import SuitVoiceConfig

        used_count = sum(
            1 for entry in SuitVoiceConfig(init_llm=False).intent_map.values()
            if str(entry.get("Used", "")).strip().lower() == "yes"
        )
        answer = QMessageBox.question(
            self, "Cache Top-Up Sequence",
            f"Fill the variation cache for up to {used_count} used voice lines\n"
            f"to {self.cache_max_spin.value()} variants each.\n\n"
            "This renders in the background and can take HOURS on CPU.\n"
            "It is incremental - stopping keeps whatever is already rendered.\n\nStart the top-up?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.topup_btn.setEnabled(False)
        self._set_status("Cache top-up running - watch the log...", "#FBBF24")
        # Read the spinbox on the GUI thread; the worker never touches widgets.
        self._topup_max = int(self.cache_max_spin.value())
        self._topup_thread = _start_thread(self, self._topup_job, self._topup_finished)
        # Top-up progress belongs in the Pipeline Log, not the setup console.
        self._topup_thread.log_line.disconnect()
        self._topup_thread.log_line.connect(self._append_topup_log)

    def _topup_job(self):
        from modular.config import SuitVoiceConfig

        self._topup_thread.emit_log(
            " [TopUp] Loading the local model for rendering (this can take a minute)..."
        )
        heavy_config = SuitVoiceConfig(init_llm=False)
        wem_ids = [
            wem_id
            for wem_id, entry in heavy_config.intent_map.items()
            if str(entry.get("Used", "")).strip().lower() == "yes"
        ]
        if not wem_ids:
            raise RuntimeError("No 'Used: yes' entries found in the intent map.")
        from extras.cache_topup_to_max import fill_cache_for_ids

        def progress(index, total, wem_id, missing):
            note = f"rendering {missing} variant(s)" if missing else "already full"
            self._topup_thread.emit_log(f" [TopUp] {index}/{total} -> WEM {wem_id} ({note})")

        fill_cache_for_ids(wem_ids, max_count=getattr(self, "_topup_max", 16), progress=progress)
        return len(wem_ids)

    def _topup_finished(self, ok: bool, error: str | None, count=None):
        self.topup_btn.setEnabled(True)
        self._set_status("Idle", "#4ADE80")
        if ok:
            QMessageBox.information(self, "Success", "Vocal variation buffer populating complete!")
        else:
            QMessageBox.critical(self, "Error", f"Cache generation sequence encountered an issue: {error}")

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
            "(Pocket TTS model weights are NOT touched.)",
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            purged = purge_cache_audio(self.window.project_root, self.window.env_data)
            QMessageBox.information(self, "Success", f"Exosuit local audio storage purged cleanly ({purged} files)!")
            self._set_status("Storage Purged Clean", "#4ADE80")
        except Exception as err:
            QMessageBox.critical(self, "Purge Error", f"Could not flush file boundaries: {err}")

    # -------------------------------------------------------- burner lock
    def _toggle_stove_burner_lock(self):
        if self.accept_responsibility.isChecked():
            self.cache_max_spin.setEnabled(True)
            self.interval_spin.setEnabled(True)
            self.warning_label.setText(self.warning_text + self.halloween_easter_egg)
            self.warning_label.setStyleSheet("color: #EF4444; font-style: italic;")
            self._set_status("LAMENT CONFIGURATION UNLOCKED.", "#EF4444")
        else:
            self.cache_max_spin.setEnabled(False)
            self.interval_spin.setEnabled(False)
            self.warning_label.setText(self._vault_brief)
            self.warning_label.setStyleSheet("color: #F87171; font-style: italic;")
            self._set_status("Idle", "#4ADE80")


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
