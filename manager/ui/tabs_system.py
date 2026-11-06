# manager/ui/tabs_system.py - System Environment tab: paths, setup, pipeline settings, cache maintenance.
import json
import os
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
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
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from manager.setup_runner import SetupRunner
from engine.path_utils import is_pipeline_running, purge_cache_audio, resolve_mod_root, resolve_mod_wem_dir


class SystemTab(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self._setup_thread = None
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
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(5)  # keep the stacked groups scrollbar-free at default heights
        right = QVBoxLayout(right_host)
        right.setContentsMargins(0, 0, 0, 0)
        root_layout.addWidget(left_scroll, 5)
        root_layout.addWidget(right_scroll, 4)

        pipeline_group = QGroupBox(" Voice Pipeline ")
        left.addWidget(pipeline_group)
        pipe_layout = QVBoxLayout(pipeline_group)

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

        paths_group = QGroupBox(" Core Platform File Infrastructure ")
        left.addWidget(paths_group)
        paths_layout = QVBoxLayout(paths_group)

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
        setup_group = QGroupBox(" First-Time Setup ")
        left.addWidget(setup_group)
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
        self.setup_hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setup_hint.setStyleSheet("color: #8a8a8a; font-style: italic;")
        setup_layout.addWidget(self.setup_hint)

        self.setup_log = QPlainTextEdit()
        self.setup_log.setReadOnly(True)
        self.setup_log.setMaximumHeight(160)
        self.setup_log.setStyleSheet("background-color: #121212; color: #00FF00; font-family: Consolas;")
        self.setup_log.setVisible(False)  # only shown while Setup actually runs
        setup_layout.addWidget(self.setup_log)

        # Troubleshooting ----------------------------------------------------
        maintenance = QGroupBox(" Troubleshooting ")
        left.addWidget(maintenance)
        maint_layout = QVBoxLayout(maintenance)

        self.purge_btn = QPushButton("Wipe Cached Voice Variants")
        self.purge_btn.clicked.connect(self._purge_audio_cache)
        self.purge_btn.setToolTip(
            "Emergency cleanup: deletes all cached voice variants and temp renders\n"
            "(model weights untouched) for a clean rebuild. Rarely needed - the\n"
            "mod manages this cache automatically and re-fills it as you play."
        )
        maint_layout.addWidget(self.purge_btn)

        # Live pipeline console: tails cache/pipeline.log
        log_group = QGroupBox(" Pipeline Log ")
        right.addWidget(log_group, 1)
        log_layout = QVBoxLayout(log_group)
        self.pipeline_log_view = QPlainTextEdit()
        self.pipeline_log_view.setReadOnly(True)
        self.pipeline_log_view.setMaximumBlockCount(400)
        self.pipeline_log_view.setStyleSheet(
            "background-color: #121212; color: #9CDCFE; font-family: Consolas;"
        )
        log_layout.addWidget(self.pipeline_log_view)

        # RIGHT PANE: advanced settings ------------------------------------
        vault_group = QGroupBox(" Advanced Settings ")
        left.addWidget(vault_group, 1)
        vault_layout = QVBoxLayout(vault_group)

        self.warning_label = QLabel(
            "These values trade CPU load, disk use and voice variety against each other.\n"
            "Defaults are recommended; changes land on Apply Changes."
        )
        self.warning_label.setWordWrap(True)
        self.warning_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.warning_label.setStyleSheet("color: #F87171; font-style: italic;")
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
        self.window.env_data.setdefault("runtime", {})["autostart_pipeline"] = bool(checked)
        try:
            self.window.env_config_file.write_text(
                json.dumps(self.window.env_data, indent=4), encoding="utf-8"
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
