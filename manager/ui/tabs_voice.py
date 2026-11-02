# manager/ui/tabs_voice.py - Pocket TTS voice roster and diagnostics.
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from modular import tts_utils
from ui.workers import FunctionThread

_POCKET_TTS_TERMS_URL = "https://huggingface.co/kyutai/pocket-tts"

_BASE_CATALOG = {
    "marius": ("Marius", "MALE"),
    "javert": ("Javert", "MALE"),
    "jean": ("Jean", "MALE"),
    "alba": ("Alba", "MALE"),
    "fantine": ("Fantine (Mod Default)", "FEMALE"),
    "cosette": ("Cosette", "FEMALE"),
    "eponine": ("Eponine", "FEMALE"),
    "azelma": ("Azelma", "FEMALE"),
}

_CLONE_NAME_INVALID = re.compile(r'[\\/:*?"<>|]')


def _sanitize_clone_name(name: str) -> str:
    """Collapse a typed voice name into a filesystem-safe key (spaces become underscores)."""
    cleaned = _CLONE_NAME_INVALID.sub("", name.strip())
    return re.sub(r"\s+", "_", cleaned)


class VoiceModelTab(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self._warm_thread = None
        self._clone_thread = None
        self._setup_thread = None
        self._clone_sample_path = None
        self._clone_auth_hint = False
        self._clone_log_tail = []
        self._build_ui()
        self._refresh_catalog()

    def _build_ui(self):
        root_layout = QHBoxLayout(self)
        left = QVBoxLayout()
        root_layout.addLayout(left, 5)

        list_group = QGroupBox(" Available Exosuit Speech Voices ")
        left.addWidget(list_group, 1)
        list_layout = QVBoxLayout(list_group)
        list_layout.addWidget(QLabel("Select Active Synthesis Voice:"))

        self.voice_list = QListWidget()
        self.voice_list.currentRowChanged.connect(self._on_selection_change)
        list_layout.addWidget(self.voice_list, 1)

        buttons = QHBoxLayout()
        self.apply_btn = QPushButton("Set as Active Voice")
        self.apply_btn.clicked.connect(self._activate_selected_voice)
        self.apply_btn.setToolTip(
            "Marks the selected voice as the one to use. Nothing is written until\n"
            "you press Apply Changes (which re-renders the mod folder to match)."
        )
        buttons.addWidget(self.apply_btn)
        self.warm_btn = QPushButton("Warm Voice Weights")
        self.warm_btn.clicked.connect(self._trigger_async_warm)
        self.warm_btn.setToolTip(
            "Pre-loads the selected voice's model weights so the first generated\n"
            "line isn't slow. Already-prepared voices skip instantly."
        )
        buttons.addWidget(self.warm_btn)
        self.delete_btn = QPushButton("Remove Voice")
        self.delete_btn.clicked.connect(self._remove_custom_voice)
        self.delete_btn.setToolTip("Deletes the selected cloned/custom voice from user/pocket_voices/.")
        buttons.addWidget(self.delete_btn)
        list_layout.addLayout(buttons)

        right = QVBoxLayout()
        root_layout.addLayout(right, 4)
        details_group = QGroupBox(" Voice Specifics & System Diagnostics ")
        right.addWidget(details_group, 1)
        details_layout = QVBoxLayout(details_group)

        self.meta_title = QLabel("No Voice Selected")
        self.meta_title.setStyleSheet("font-weight: bold;")
        details_layout.addWidget(self.meta_title)
        self.meta_status = QLabel("Status: Ready")
        self.meta_status.setStyleSheet("font-weight: bold; color: #94A3B8;")
        details_layout.addWidget(self.meta_status)
        self.meta_desc = QLabel("Select a voice from the list to adjust active suit weights.")
        self.meta_desc.setWordWrap(True)
        self.meta_desc.setStyleSheet("font-style: italic; color: #8a8a8a;")
        details_layout.addWidget(self.meta_desc)
        details_layout.addStretch(1)

        self.download_progress = QLabel("Download Queue: Idle")
        self.download_progress.setStyleSheet("font-family: Consolas; color: #94A3B8;")
        # Long status text must wrap instead of widening the right column and
        # squeezing the voice list (the column-width jump seen during clones).
        self.download_progress.setWordWrap(True)
        self.download_progress.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        details_layout.addWidget(self.download_progress)

        clone_group = QGroupBox(" Clone a Voice ")
        right.addWidget(clone_group)
        clone_layout = QVBoxLayout(clone_group)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name:"))
        self.clone_name_edit = QLineEdit()
        self.clone_name_edit.setPlaceholderText("myvoice")
        self.clone_name_edit.setToolTip('Spaces become underscores; \\ / : * ? " < > | are not allowed.')
        self.clone_name_edit.textChanged.connect(self._update_clone_button)
        name_row.addWidget(self.clone_name_edit, 1)
        clone_layout.addLayout(name_row)
        self.clone_name_hint = QLabel("")
        self.clone_name_hint.setStyleSheet("color: #8a8a8a; font-style: italic;")
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
        self.clone_sample_label.setStyleSheet("color: #8a8a8a;")
        self.clone_sample_label.setWordWrap(True)
        self.clone_sample_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        sample_row.addWidget(self.clone_sample_label, 1)
        clone_layout.addLayout(sample_row)

        self.clone_btn = QPushButton("Clone Voice")
        self.clone_btn.setEnabled(False)
        self.clone_btn.setToolTip(
            "Builds a suit voice from the sample. First use downloads the full\n"
            "cloning model once (online); the voice then appears in the list."
        )
        self.clone_btn.clicked.connect(self._trigger_async_clone)
        clone_layout.addWidget(self.clone_btn)

        setup_group = QGroupBox(" First-time Cloning Setup ")
        self.setup_group = setup_group
        right.addWidget(setup_group)
        setup_layout = QVBoxLayout(setup_group)
        setup_hint = QLabel("The first clone needs a one-time Hugging Face sign-in and model download.")
        setup_hint.setWordWrap(True)
        setup_hint.setStyleSheet("color: #8a8a8a; font-style: italic;")
        setup_layout.addWidget(setup_hint)
        row = QHBoxLayout()
        self.terms_btn = QPushButton("Open Terms Page")
        self.terms_btn.setToolTip("Accept the model's terms on huggingface.co (free account).")
        self.terms_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(_POCKET_TTS_TERMS_URL)))
        row.addWidget(self.terms_btn)
        self.setup_btn = QPushButton("Sign in && Preload Model")
        self.setup_btn.setToolTip(
            "Opens a Hugging Face sign-in window, saves the token for cloning and\n"
            "pre-downloads the cloning model so Clone Voice never waits on it."
        )
        self.setup_btn.clicked.connect(self._trigger_voice_setup)
        row.addWidget(self.setup_btn)
        setup_layout.addLayout(row)

    # -------------------------------------------------------------- catalog
    def _pocket_model(self) -> str:
        """Configured Pocket TTS model key, falling back to the code default."""
        try:
            env_data = json.loads(self.window.env_config_file.read_text(encoding="utf-8"))
            return env_data.get("models", {}).get("pocket_model") or tts_utils.POCKET_DEFAULT_MODEL
        except Exception:
            return tts_utils.POCKET_DEFAULT_MODEL

    def _catalog(self):
        catalog = {key: dict(zip(("name", "gender"), value)) for key, value in _BASE_CATALOG.items()}
        model_name = self._pocket_model()
        for voice in tts_utils.list_pocket_voices(self.window.project_root, model_name):
            if voice not in catalog:
                catalog[voice] = {
                    "name": voice, "gender": "CLONED",
                    "note": "Custom cloned voice loaded from user/pocket_voices/.",
                }
        return catalog

    def _active_voice(self):
        return self.window.user_data.get("tts_voice") or tts_utils.POCKET_DEFAULT_VOICE

    def _refresh_catalog(self):
        self._pending_voice = None  # a refresh means the committed state is the truth
        self.voice_list.blockSignals(True)
        self.voice_list.clear()
        catalog = self._catalog()
        active = self._active_voice()
        for key, info in catalog.items():
            ready = self._voice_ready(key)
            tag = "[Ready]" if ready else "[No State]"
            if key == active:
                tag = "\u25b6 [ACTIVE ON EXOSUIT]"
            self.voice_list.addItem(f" {tag} {info['name']}")
        self.voice_list.blockSignals(False)
        # preselect the active voice row
        for index in range(self.voice_list.count()):
            if "\u25b6" in self.voice_list.item(index).text():
                self.voice_list.setCurrentRow(index)
                break
        self._update_setup_visibility()

    def _cloning_ready(self) -> bool:
        """True when cloning can proceed without the setup button: the gated
        cloning model is cached locally, or a Hugging Face credential exists."""
        if os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN"):
            return True
        if (Path.home() / ".cache" / "huggingface" / "token").is_file():
            return True
        if (self.window.project_root / "cache" / "token").is_file():
            return True
        model_key = self._pocket_model()
        hub = self.window.project_root / "cache" / "hub"
        return any(
            hub.glob(f"models--kyutai--pocket-tts/snapshots/*/languages/{model_key}/*.safetensors"))

    def _update_setup_visibility(self):
        """The first-time setup group only shows when cloning is not ready yet."""
        self.setup_group.setVisible(not self._cloning_ready())

    def _voice_ready(self, key: str) -> bool:
        if key in tts_utils.PREDEFINED_POCKET_VOICES:
            return True
        return tts_utils.pocket_custom_voice_state_path(
            self.window.project_root, key, self._pocket_model()
        ) is not None

    def _selected_key(self) -> str | None:
        row = self.voice_list.currentRow()
        if row < 0:
            return None
        text = self.voice_list.item(row).text()
        display_name = text.split("] ", 1)[-1].strip()
        for key, info in self._catalog().items():
            if info["name"] == display_name:
                return key
        return None

    # ---------------------------------------------------------- interaction
    def _on_selection_change(self, _row):
        key = self._selected_key()
        if key is None:
            return
        info = self._catalog()[key]
        self.meta_title.setText(info["name"])
        self.meta_desc.setText(
            f"\u2022 Voice Identifier:\n  {key}\n\n"
            f"\u2022 Vocal Baseline Profile: [{info['gender']}]"
        )
        ready = self._voice_ready(key)
        active = (self._active_voice() == key)
        if active:
            self.meta_status.setText("Status: Active and Loaded on Exosuit Matrix")
            self.meta_status.setStyleSheet("font-weight: bold; color: #4ADE80;")
            self.apply_btn.setEnabled(False)
        elif ready:
            if key == getattr(self, "_pending_voice", None):
                self.meta_status.setText("Status: Selected - applies when you press Apply Changes")
                self.meta_status.setStyleSheet("font-weight: bold; color: #60A5FA;")
            else:
                self.meta_status.setText("Status: Ready on Local Disk")
                self.meta_status.setStyleSheet("font-weight: bold; color: #FBBF24;")
            self.apply_btn.setEnabled(True)
        else:
            self.meta_status.setText("Status: Not Cloned (State Missing)")
            self.meta_status.setStyleSheet("font-weight: bold; color: #F87171;")
            self.apply_btn.setEnabled(False)
        self.warm_btn.setEnabled(True)
        self.delete_btn.setEnabled(key not in _BASE_CATALOG)

    def _activate_selected_voice(self):
        key = self._selected_key()
        if key is None:
            return
        # Pending only: Apply Changes commits tts_voice (one re-record sweep, like tone/verbiage).
        self._pending_voice = key
        self._on_selection_change(self.voice_list.currentRow())

    def _select_by_key(self, key: str) -> bool:
        """Move the roster selection to key (used when a preset switches the audition voice)."""
        info = self._catalog().get(key)
        if not info:
            return False
        for index in range(self.voice_list.count()):
            if self.voice_list.item(index).text().split("] ", 1)[-1].strip() == info["name"]:
                self.voice_list.setCurrentRow(index)
                return True
        return False

    def _trigger_async_warm(self):
        key = self._selected_key()
        if key is None:
            return
        self.warm_btn.setEnabled(False)
        self.download_progress.setText("Queue Status: Loading engine weights (Please wait)...")
        # NOTE: pass a callable, not the call result - the warm-up is slow and
        # must run on the worker thread, not freeze the GUI thread.
        self._warm_thread = FunctionThread(lambda: self._warm_job(key), self)
        self._warm_thread.finished_ok.connect(lambda _r: self._warm_finished(None, key))
        self._warm_thread.failed.connect(lambda err: self._warm_finished(err, key))
        self._warm_thread.start()

    def _warm_job(self, key: str):
        tts_utils.warm_pocket_engine(self._pocket_model(), self.window.project_root, key)

    def _warm_finished(self, error: str | None, key: str):
        self.warm_btn.setEnabled(True)
        if error is None:
            self.download_progress.setText("Queue Status: Voice weights verified successfully.")
            QMessageBox.information(self, "Success", "Voice engine parameters compiled and verified safely on your drive.")
            self._refresh_catalog()
        else:
            self.download_progress.setText("Queue Status: Download process failed.")
            QMessageBox.critical(self, "Network Error", f"Vocal package download sequence encountered an issue:\n{error}")

    def _remove_custom_voice(self):
        key = self._selected_key()
        if key is None or key in _BASE_CATALOG:
            return
        if QMessageBox.question(
            self, "Confirm Deletion",
            f"Permanently delete the cloned voice '{key}' from\n"
            f"user/pocket_voices/?\n\nThis cannot be undone.",
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            voice_folder = Path(self.window.project_root) / "user" / "pocket_voices" / key
            if voice_folder.exists():
                shutil.rmtree(voice_folder)
            self._refresh_catalog()
            QMessageBox.information(self, "Success", "Cloned voice removed cleanly.")
        except Exception as e:
            QMessageBox.critical(self, "Purge Failure", f"Could not remove voice folder modules: {e}")

    # ---------------------------------------------------- first-time setup
    def _setup_emit(self, message: str):
        """Thread-safe phase text into the download progress label."""
        if self._setup_thread is not None:
            self._setup_thread.emit_log(str(message)[:160])

    def _trigger_voice_setup(self):
        """One-time Hugging Face sign-in + cloning-model preload."""
        if self._setup_thread is not None:
            return
        self.setup_btn.setEnabled(False)
        self.download_progress.setText("Setup: opening the Hugging Face sign-in window...")
        self._setup_thread = FunctionThread(self._voice_setup_job, self)
        self._setup_thread.log_line.connect(self.download_progress.setText)
        self._setup_thread.finished_ok.connect(lambda _r: self._setup_finished(None))
        self._setup_thread.failed.connect(self._setup_finished)
        self._setup_thread.start()

    def _voice_setup_job(self):
        py_exe = Path(sys.executable).with_name("python.exe")
        if not py_exe.is_file():
            py_exe = Path(sys.executable)
        # 1. Interactive login in its own console (the manager runs windowless).
        self._setup_emit("A sign-in window has opened - paste your Hugging Face access token there.")
        proc = subprocess.Popen(
            [str(py_exe), "-m", "huggingface_hub.commands.huggingface_cli", "login"],
            cwd=str(self.window.project_root),
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        rc = proc.wait()
        if rc != 0:
            self._setup_emit(f"Sign-in window exited with code {rc} (you can retry).")
        # 2. Bridge the token into the project HF cache (HF_HOME-derived token path).
        sys_tok = Path.home() / ".cache" / "huggingface" / "token"
        loc_tok = self.window.project_root / "cache" / "token"
        if sys_tok.is_file() and not loc_tok.exists():
            loc_tok.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sys_tok, loc_tok)
        self._setup_emit("Token saved for cloning.")
        # 3. Preload the pinned cloning model into the project cache (streams progress).
        model_key = self._pocket_model()
        root = self.window.project_root
        code = (
            "import os; from pathlib import Path; "
            f"root = Path({str(root)!r}); p = root / 'cache'; "
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
                self._setup_emit(line.rstrip())
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"Model preload failed (exit code {proc.returncode})")

    def _setup_finished(self, error):
        self._setup_thread = None
        self.setup_btn.setEnabled(True)
        if error is None:
            self.download_progress.setText("Setup Queue: Sign-in and model preload complete.")
            QMessageBox.information(
                self, "Success",
                "Hugging Face sign-in saved and the cloning model is preloaded.\n"
                "Clone Voice is ready to run fully offline from here on.")
            self._refresh_catalog()
        else:
            self.download_progress.setText("Setup Queue: Failed.")
            QMessageBox.critical(self, "Setup Failed", f"Voice cloning setup failed:\n{error}")

    # --------------------------------------------------------------- cloning
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
            self.clone_name_hint.setStyleSheet("color: #FBBF24; font-style: italic;")
            self.clone_name_hint.setText('That name contains a character Windows paths cannot use: \\ / : * ? " < > |')
        elif valid and _sanitize_clone_name(name) != name:
            self.clone_name_hint.setStyleSheet("color: #8a8a8a; font-style: italic;")
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
        if name in _BASE_CATALOG or name in tts_utils.PREDEFINED_POCKET_VOICES:
            QMessageBox.critical(self, "Clone Error", f"'{name}' is a built-in voice; choose another name.")
            return
        model = self._pocket_model()
        dest = self.window.project_root / "user" / "pocket_voices" / name / "_model_states" / f"{model}.safetensors"
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
        self.download_progress.setText("Clone Queue: Preparing clone...")
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
        env.setdefault("HF_HOME", str(self.window.project_root / "cache"))
        hub_dir = self.window.project_root / "cache" / "hub"
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
        self.download_progress.setText(f"Clone Queue: {phase}")
        if "voice cloning" in lowered:
            self._clone_auth_hint = True

    def _clone_finished(self, error, name):
        self._clone_thread = None
        self._update_clone_button()
        if error is None:
            self.download_progress.setText("Clone Queue: Voice ready.")
            self._refresh_catalog()
            QMessageBox.information(
                self, "Success",
                f"Cloned voice '{name}' is ready.\n\n"
                "Select it and press Apply Changes to make it your suit's voice.",
            )
        else:
            self.download_progress.setText("Clone Queue: Failed.")
            tail = "\n".join(self._clone_log_tail[-4:])
            hint = ""
            if self._clone_auth_hint:
                self.setup_group.setVisible(True)  # one click from fixed
                hint = ("\n\nPress 'Sign in & Preload Model' in the Voice Model tab\n"
                        "(one-time Hugging Face sign-in), then retry.")
            QMessageBox.critical(
                self, "Clone Failed",
                f"Cloning '{name}' failed:\n{error}\n\nLast log lines:\n{tail}{hint}",
            )
