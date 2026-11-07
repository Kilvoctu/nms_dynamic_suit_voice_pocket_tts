# manager/ui/tabs_audio.py - effect sliders, character presets and the live sandbox.
import json
import subprocess
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ui.workers import FunctionThread

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

_NO_PRESET = "(no preset - custom voice)"

_SLIDERS = [
    # (attr, label, min, max, step, default_key, default, tooltip)
    ("gain", "Vocal Volume Boost (dB Gain) [Baseline: 5]", -10, 20, 1, "gain_db", 5,
     "If the voice seems too quiet within the game, increase the Gain value. Higher values can increase distortion."),
    ("tempo", "Sentence Pacing (Speech Speed) [Baseline: 1.02]", 50, 200, 1, "tempo", 102,
     "Adjust the pace of the synthesized voice without affecting the pitch."),
    ("pitch", "Core Voice Tone (Pitch & Speed Shift) [Baseline: 4]", -12, 12, 1, "pitch_semitones", 4,
     "Adjusts the vocal pitch up or down without changing the speed."),
    ("mix", "Synthetic Cybernetic Overlay % [Baseline: 100]", 0, 100, 5, "ring_mix", 100,
     "Blend unprocessed audio with post-processed audio: 0% is no post-processing effects at all, 100% is full robotic style distortion."),
    ("rate", "Audio Frequency Warp (Chipmunk Scalar) [Baseline: 0.50]", 10, 150, 1, "rate", 50,
     "Alters pitch and speed simultaneously (aka Chipmunk effect). Acts as an additional distortion layer over the vocal baseline."),
    ("freq", "Modulation Carrier Wave Frequency (Hz) [Baseline: 300]", 50, 1000, 10, "ring_freq", 300,
     "Adjust the frequency of the Robotic distortion effect. To remove it completely, set the Synthetic Cybernetic Overlay to 0."),
    ("formant", "Vocal Resonator Bandwidth (Throat Size %) [Baseline: 80]", 10, 150, 5, "formant_percent", 80,
     "Adjust how deep/gravelly the voice sounds."),
]

# slider ticks -> real values (tempo/rate are x100 on the slider)
_VALUE_SCALE = {"tempo": 0.01, "rate": 0.01}


class AudioTab(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.effects_data = window.user_data.get("voice_effects", {})
        self.presets_file = window.project_root / "assets" / "data" / "audio_presets.json"
        self.user_presets_file = window.project_root / "state" / "user" / "audio_presets.json"
        self.loaded_presets = {}
        if self.presets_file.exists():
            try:
                with open(self.presets_file, "r", encoding="utf-8") as f:
                    self.loaded_presets = json.load(f)
            except Exception as e:
                print(f"Preset Loader: Failed to parse audio_presets.json: {e}")
        # User presets (personal, untracked) layer on top; user entries win on
        # name collisions but the built-in entries themselves stay immutable.
        self.user_presets = {}
        self.user_preset_names = set()
        if self.user_presets_file.exists():
            try:
                with open(self.user_presets_file, "r", encoding="utf-8") as f:
                    self.user_presets = json.load(f)
                self.loaded_presets.update(self.user_presets)
                self.user_preset_names = set(self.user_presets)
            except Exception as e:
                print(f"Preset Loader: Failed to parse user audio_presets.json: {e}")
        self._test_thread = None
        self._build_ui()

    def _build_ui(self):
        root_layout = QHBoxLayout(self)
        left = QVBoxLayout()
        root_layout.addLayout(left, 5)

        # Presets ------------------------------------------------------
        presets = QGroupBox(" Quick Exosuit Character Profiles ")
        left.addWidget(presets)
        presets_layout = QVBoxLayout(presets)
        presets_layout.addWidget(QLabel("Precalibrated Vocal Signatures:"))
        self.preset_combo = QComboBox()
        self.preset_combo.addItem(_NO_PRESET)
        self.preset_combo.addItems(list(self.loaded_presets.keys()))
        self.preset_combo.currentTextChanged.connect(self._on_preset_selected)
        presets_layout.addWidget(self.preset_combo)
        preset_buttons = QHBoxLayout()
        self.save_preset_btn = QPushButton("Save Preset...")
        self.save_preset_btn.setToolTip(
            "Snapshot the current slider settings and the active voice as a\n"
            "reusable preset, saved to your personal (untracked) settings."
        )
        self.save_preset_btn.clicked.connect(self._save_user_preset)
        preset_buttons.addWidget(self.save_preset_btn)
        self.delete_preset_btn = QPushButton("Delete Preset")
        self.delete_preset_btn.setToolTip(
            "Removes the selected user preset. Built-in presets are immutable."
        )
        self.delete_preset_btn.clicked.connect(self._delete_user_preset)
        preset_buttons.addWidget(self.delete_preset_btn)
        presets_layout.addLayout(preset_buttons)
        self.delete_preset_btn.setEnabled(False)

        # Personal output volume - deliberately its own block: selecting a
        # preset syncs the character sliders but never touches this one.
        volume = QGroupBox(" Output Volume (Personal - Presets Leave This Untouched) ")
        left.addWidget(volume)
        volume_layout = QVBoxLayout(volume)

        # Sliders ------------------------------------------------------
        primary = QGroupBox(" Primary Exosuit Audio Tuning ")
        left.addWidget(primary)
        primary_layout = QVBoxLayout(primary)
        advanced = QGroupBox(" Advanced Signal Diagnostics (Fine Tuning) ")
        left.addWidget(advanced)
        advanced_layout = QVBoxLayout(advanced)
        group_for = {"gain": volume_layout, "tempo": primary_layout,
                     "pitch": primary_layout, "mix": primary_layout,
                     "rate": advanced_layout, "freq": advanced_layout,
                     "formant": advanced_layout}

        self.sliders = {}
        self.slider_value_labels = {}
        for attr, label, lo, hi, step, key, default, tooltip in _SLIDERS:
            group_for[attr].addWidget(QLabel(label))
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(lo, hi)
            slider.setSingleStep(step)
            value = self.effects_data.get(key, default)
            if attr in _VALUE_SCALE:
                value = int(round(float(value) / _VALUE_SCALE[attr]))
            slider.setValue(int(value))
            slider.setTickPosition(QSlider.TickPosition.TicksBelow)
            slider.setTickInterval(max(step, 1))
            slider.setToolTip(tooltip)
            slider_row = QHBoxLayout()
            slider_row.addWidget(slider, 1)
            value_label = QLabel()
            value_label.setStyleSheet("font-family: Consolas; color: #9CDCFE;")
            value_label.setFixedWidth(48)
            value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            slider_row.addWidget(value_label)
            group_for[attr].addLayout(slider_row)
            slider.valueChanged.connect(lambda _v, a=attr: self._update_slider_label(a))
            self.sliders[attr] = (slider, key)
            self.slider_value_labels[attr] = value_label
            self._update_slider_label(attr)

        left.addStretch(1)

        # Sandbox ------------------------------------------------------
        sandbox = QGroupBox(" Real-Time Synthesis Sandbox ")
        root_layout.addWidget(sandbox, 4)
        sandbox_layout = QVBoxLayout(sandbox)

        advisory = QLabel(
            "\u2022 INITIALIZATION PAUSE: clicking 'Apply Effects' may take a minute or more to boot the local speech model.\n"
            "\u2022 The voice itself is managed in the Voice Model tab."
        )
        advisory.setWordWrap(True)
        advisory.setStyleSheet("color: #8a8a8a; font-style: italic;")
        sandbox_layout.addWidget(advisory)

        self.raw_voice_btn = QPushButton("Raw Voice (No Effects)")
        self.raw_voice_btn.setToolTip(
            "Dials every effect to its bypass point so the voice's natural sound\n"
            "comes through (pitch 0, formant 100, mix 0, rate 0.54, tempo 1.00).\n"
            "Your personal volume boost is left untouched."
        )
        self.raw_voice_btn.clicked.connect(self._apply_raw_voice)
        sandbox_layout.addWidget(self.raw_voice_btn)

        sandbox_layout.addWidget(QLabel("Input Live Diagnostic Test Phrase:"))
        self.test_text = QPlainTextEdit()
        self.test_text.setPlainText("This is a test of how the Dynamic Suit Voice will sound in the game.")
        self.test_text.setMaximumHeight(80)
        sandbox_layout.addWidget(self.test_text)

        self.generate_btn = QPushButton("Apply Effects && Play Audio")
        self.generate_btn.clicked.connect(self._run_async_audio_test)
        sandbox_layout.addWidget(self.generate_btn)

        sandbox_layout.addWidget(QLabel("Tuning Engine Pipeline Output:"))
        self.status_log = QPlainTextEdit()
        self.status_log.setReadOnly(True)
        self.status_log.setStyleSheet("background-color: #121212; color: #00FF00; font-family: Consolas;")
        sandbox_layout.addWidget(self.status_log, 1)
        self._log(" TTO Engine Status: Idle\n Ready to synthesize testing streams.")

        # Last: sync the preset dropdown + sliders to the active voice (needs
        # sliders and the status log to exist, since signals fire through).
        self._preselect_active_preset()

    # ------------------------------------------------------------ logging
    def _log(self, message: str):
        self.status_log.appendPlainText(message)

    def _update_slider_label(self, attr: str):
        """Show the slider's live real-world value next to it (scaled sliders display true units)."""
        slider, _key = self.sliders[attr]
        value = slider.value()
        if attr in _VALUE_SCALE:
            text = f"{value * _VALUE_SCALE[attr]:.2f}"
        else:
            text = str(value)
        self.slider_value_labels[attr].setText(text)

    # ------------------------------------------------------------ presets
    def _preselect_active_preset(self):
        """Preselect the preset matching the active voice (sliders tell the truth
        on launch); a fresh install with no committed voice defaults to Factory Standard."""
        current_voice = self.window.user_data.get("tts_voice", "")
        if not current_voice:
            name = "Factory Standard Exosuit Core"
            if name in self.loaded_presets:
                # Signals blocked: the voice tab may not exist yet during
                # construction, so the preset's voice-switch step must not run.
                self.preset_combo.blockSignals(True)
                self.preset_combo.setCurrentText(name)
                self.preset_combo.blockSignals(False)
                self._apply_preset_values(self.loaded_presets[name])
            return
        for name, preset in self.loaded_presets.items():
            if preset.get("tts_voice") == current_voice:
                self.preset_combo.setCurrentText(name)
                return

    def _apply_preset_values(self, p: dict):
        """Dial the character sliders to a preset's values (personal volume untouched)."""
        values = {
            "pitch": p.get("pitch_semitones", 4),
            "tempo": p.get("tempo", 1.02),
            "rate": p.get("rate", 0.50),
            "freq": p.get("ring_freq", 300),
            "mix": p.get("ring_mix", 100),
            "formant": p.get("formant_percent", 80),
        }
        for attr, value in values.items():
            slider, _key = self.sliders[attr]
            if attr in _VALUE_SCALE:
                value = int(round(float(value) / _VALUE_SCALE[attr]))
            slider.setValue(int(value))

    def _on_preset_selected(self, name: str):
        self.delete_preset_btn.setEnabled(name in self.user_preset_names)
        p = self.loaded_presets.get(name)
        if not p:
            return
        self._apply_preset_values(p)

        # A vocal signature is more than sliders: switch the audition voice too
        # (roster selection only - Apply Changes commits it to the engine).
        preset_voice = p.get("tts_voice")
        if preset_voice and self.window.audition_voice() != preset_voice:
            if self.window.select_voice_in_roster(preset_voice):
                self._log(f" Voice signature switched to '{preset_voice}'. "
                          "Apply Changes to commit it to the running engine.")
            else:
                self._log(f" Preset voice '{preset_voice}' is not in the roster.")

        self._log(f" {p.get('description', 'Preset loaded.')}")

    # ------------------------------------------------------ user presets
    def _rebuild_preset_combo(self, select: str = ""):
        self.preset_combo.blockSignals(True)
        current = select or self.preset_combo.currentText()
        self.preset_combo.clear()
        self.preset_combo.addItem(_NO_PRESET)
        self.preset_combo.addItems(list(self.loaded_presets.keys()))
        idx = self.preset_combo.findText(current)
        self.preset_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.preset_combo.blockSignals(False)
        self.delete_preset_btn.setEnabled(current in self.user_preset_names)

    def _save_user_preset(self):
        name, ok = QInputDialog.getText(self, "Save Preset", "Preset name:")
        if not ok:
            return
        name = name.strip()
        if not name or name == _NO_PRESET:
            return
        if name in self.loaded_presets and name not in self.user_preset_names:
            QMessageBox.critical(
                self, "Save Preset", f"'{name}' is a built-in preset; pick another name."
            )
            return
        voice = self.window.audition_voice()
        values = self._slider_values()
        values.pop("gain_db", None)  # output volume stays personal, like the built-ins
        self.user_presets[name] = {"description": "User preset.", "tts_voice": voice, **values}
        self.loaded_presets[name] = self.user_presets[name]
        self.user_preset_names.add(name)
        try:
            self.user_presets_file.parent.mkdir(parents=True, exist_ok=True)
            self.user_presets_file.write_text(
                json.dumps(self.user_presets, indent=4), encoding="utf-8"
            )
        except OSError as e:
            QMessageBox.critical(self, "Save Preset", f"Could not write the preset file:\n{e}")
            return
        self._rebuild_preset_combo(select=name)
        self._log(f" Preset '{name}' saved (voice: {voice or 'unchanged'}).")

    def _delete_user_preset(self):
        name = self.preset_combo.currentText()
        if name not in self.user_preset_names:
            return
        if QMessageBox.question(
            self, "Delete Preset", f"Delete user preset '{name}'?"
        ) != QMessageBox.StandardButton.Yes:
            return
        self.user_presets.pop(name, None)
        self.user_preset_names.discard(name)
        self.loaded_presets.pop(name, None)
        try:
            self.user_presets_file.write_text(
                json.dumps(self.user_presets, indent=4), encoding="utf-8"
            )
        except OSError as e:
            QMessageBox.critical(self, "Delete Preset", f"Could not write the preset file:\n{e}")
            return
        self._rebuild_preset_combo(select=_NO_PRESET)
        self._log(f" Preset '{name}' deleted.")

    def reset_to_sane_defaults(self):
        defaults = {"gain": 5, "tempo": 102, "pitch": 4, "mix": 100,
                    "rate": 50, "freq": 300, "formant": 80}
        for attr, value in defaults.items():
            self.sliders[attr][0].setValue(value)
        self._log(" Matrix reset cleanly to factory defaults.")

    def _apply_raw_voice(self):
        """Dial every character slider to its bypass point (no DSP coloring)."""
        neutral = {"pitch": 0, "tempo": 100, "mix": 0, "rate": 54, "freq": 300, "formant": 100}
        for attr, value in neutral.items():
            self.sliders[attr][0].setValue(value)
        self.preset_combo.setCurrentText(_NO_PRESET)
        self._log(" Raw voice: all effects bypassed (personal volume untouched). "
                  "Use Save Preset to keep this as a preset.")

    # ------------------------------------------------------ sandbox test
    def _slider_values(self):
        fx = {}
        for attr, (slider, key) in self.sliders.items():
            value = slider.value()
            if attr in _VALUE_SCALE:
                value = round(value * _VALUE_SCALE[attr], 2)
            fx[key] = value
        return fx

    def _run_async_audio_test(self):
        self.generate_btn.setEnabled(False)
        # Capture ALL widget state on the GUI thread before the worker starts:
        # Qt widgets must never be touched from a worker thread (Qt6Gui crashes).
        self._test_inputs = {
            "text_payload": self.test_text.toPlainText().strip(),
            "fx": self._slider_values(),
            "voice": self.window.audition_voice(),
        }
        self._log(" Starting background voice synthesis sequence...")
        self._test_thread = FunctionThread(self._audio_test_job, self)
        self._test_thread.log_line.connect(self._log)
        self._test_thread.finished_ok.connect(lambda _r: self._test_finished(None))
        self._test_thread.failed.connect(self._test_finished)
        self._test_thread.start()

    def _audio_test_job(self):
        from engine.config import SuitVoiceConfig
        from engine import tts_utils
        from engine.audio_chain import render_chain, write_wav_float

        inputs = getattr(self, "_test_inputs", {})
        text_payload = inputs.get("text_payload", "")
        fx = dict(inputs.get("fx", {}))
        voice = inputs.get("voice") or tts_utils.POCKET_DEFAULT_VOICE

        self._test_thread.emit_log(" Connecting to Pocket TTS Engine...")
        heavy_cfg = SuitVoiceConfig(init_llm=False)
        fx.setdefault("loudness_target", float(heavy_cfg.voice_effects.get("loudness_target", -5.0)))
        fx.setdefault("loudness_true_peak", float(heavy_cfg.voice_effects.get("loudness_true_peak", 5.0)))

        cache_dir = self.window.project_root / "state" / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        final_wav = cache_dir / "sandbox_processed.wav"
        self._test_thread.emit_log(f" Synthesizing vocal wave footprints ({voice})...")
        audio, sr = tts_utils.pocket_generate(
            heavy_cfg.pocket_model_name, heavy_cfg.root_dir, text_payload, voice,
            max_tokens=heavy_cfg.pocket_tts_max_tokens,
        )

        self._test_thread.emit_log(" Injecting authentic local numpy robotic filter matrices (sculpt, tempo, rate, loudness)...")
        audio, sr = render_chain(audio, sr, fx)
        write_wav_float(final_wav, audio, sr)

        ffplay_bin = str(heavy_cfg.ffmpeg_path.parent / "ffplay.exe")
        self._test_thread.emit_log(" Footprints generated successfully! Triggering playback stream...")
        # Play at reduced volume: the sandbox auditions the exact game-bound audio,
        # but the game's own voice bus adds attenuation a raw desktop playback lacks.
        subprocess.run([
            ffplay_bin, "-nodisp", "-autoexit", "-volume", "35", str(final_wav)
        ], check=True, creationflags=_CREATE_NO_WINDOW)
        self._test_thread.emit_log(" Playback loop finished smoothly. Tuning complete.")

    def _test_finished(self, error: str | None):
        self.generate_btn.setEnabled(True)
        if error:
            self._log(f"\n ERROR: Synthesis pipeline failed:\n {error}")
            QMessageBox.critical(self, "Sandbox Failure", f"Vocal processing chain error:\n{error}")
