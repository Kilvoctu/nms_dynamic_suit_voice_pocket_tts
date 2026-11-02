# config.py
import json
import os
import shutil
import sys
from pathlib import Path
from llama_cpp import Llama, Optional
from modular import tts_utils
from modular import prompt_presets
from modular.prompt_utils import build_promptdata
from modular.path_utils import resolve_mod_wem_dir

def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result

class SuitVoiceConfig:
    def __init__(self, init_llm: bool = True):
        # Anchor to project root (parent of modular/)
        root_dir = Path(__file__).parent.parent
        self.root_dir = root_dir
        config_dir = root_dir / "config"

        # Load file structures
        self.environment = load_json(config_dir / "environment_config.json")
        self.llm_prompting = load_json(config_dir / "llm_prompting.json")
        self.default_customization = load_json(config_dir / "default_customization.json")
        # Personal settings live untracked in user/; fall back to the legacy
        # config/ location so existing installs migrate on first save.
        legacy_user = config_dir / "user_customization.json"
        self.user_customization_path = root_dir / "user" / "user_customization.json"
        if not self.user_customization_path.is_file() and legacy_user.is_file():
            self.user_customization_path = legacy_user
        self.user_customization = load_json(self.user_customization_path)

        # Merge defaults with custom user tweaks cleanly
        self.customization = deep_merge(
            self.default_customization,
            self.user_customization
        )

        # --- RUNTIME SETTINGS ---
        runtime = self.environment.get("runtime", {})
        self.check_interval = float(runtime.get("check_interval", 0.1))
        self.max_session_lines = int(runtime.get("max_session_lines", 10))
        self.quick_cache_max = int(runtime.get("quick_cache_max", 16))

        # ====================================================
        # CORE FILESYSTEM PATHS (user config first; env config holds shipped defaults)
        paths = self.environment.get("paths", {})

        # Game Mod Directory (tolerant load: empty/missing before setup). Accepts any
        # granularity and resolves to the flat ENGLISH(US) folder the watchdog monitors.
        user_paths = self.customization.get("paths", {})
        mod_dir_setting = user_paths.get("mod_dir") or paths.get("mod_dir")
        self.mod_dir = resolve_mod_wem_dir(mod_dir_setting) if mod_dir_setting else None

        # Tray App UI Icon (Force to clean string format for PIL/pystray compatibility)
        icon_path_obj = self.resolve_json_path(paths.get("icon_image"), root_dir, must_exist=True)
        self.icon_image = str(icon_path_obj)

        # Engine Cache Locations (Will be auto-provisioned safely below)
        self.temp_wem_dir = self.resolve_json_path(paths.get("temp_wem_dir"), root_dir, must_exist=False)
        self.temp_wav_dir = self.resolve_json_path(paths.get("temp_wav_dir"), root_dir, must_exist=False)
        self.quick_cache_dir = self.resolve_json_path(paths.get("quick_cache_dir"), root_dir, must_exist=False)


        # Window platform subprocess flags
        self.create_no_window = 0x08000000 if sys.platform == "win32" else 0

        # Enforce workspace directories (Safe loop: all paths are strict non-nullable Path types)
        for d in [self.temp_wem_dir, self.temp_wav_dir, self.quick_cache_dir]:
            d.mkdir(parents=True, exist_ok=True)

        # ====================================================
        # EXTERNAL SYSTEM UTILITIES (environment_config -> "external_tools")
        ext_tools = self.environment.get("external_tools", {})
        self.wav2wem_path = self.resolve_json_path(ext_tools.get("wav2wem_path"), root_dir, must_exist=True)
        self.wav2wem_quality = ext_tools.get("wav2wem_quality", 6)

        # --- PROMPT ASSETS & FILES ---
        files = self.llm_prompting.get("files", {})
        self.transcripts_path = self.resolve_json_path(files.get("transcripts_path"), root_dir, must_exist=True) # JSON for intent mapping
        self.intent_map = json.loads(self.transcripts_path.read_text(encoding="utf-8")) # JSON for intent mapping

        self.suit_voice_base_path = self.resolve_json_path(files.get("base_prompt"), root_dir, must_exist=True)
        self.suit_voice_base = self.suit_voice_base_path.read_text(encoding="utf-8")
        self.suit_voice_base_builtin = self.suit_voice_base  # pristine built-in for base-prompt switching

        self.suit_voice_dynamic_path = self.resolve_json_path(files.get("dynamic_prompt"), root_dir, must_exist=True)
        self.suit_voice_dynamic = self.suit_voice_dynamic_path.read_text(encoding="utf-8")

        self.suit_voice_combat_path = self.resolve_json_path(files.get("combat_prompt"), root_dir, must_exist=True)
        self.suit_voice_combat = self.suit_voice_combat_path.read_text(encoding="utf-8")

        self.tokenized_logits_path = self.resolve_json_path(files.get("tokenized_logits_path"), root_dir, must_exist=True)
        self.logit_banlist = json.loads(self.tokenized_logits_path.read_text(encoding="utf-8"))

        # --- COMBAT / CATEGORY METRIC SETTING ---
        combat_cfg = self.llm_prompting.get("combat", {})
        self.mil_cat = combat_cfg.get("special_categories", [])

        labels = self.llm_prompting.get("category_labels", {})
        self.units_received = labels.get("units_received", "Increased Funds")
        self.units_insufficient = labels.get("units_insufficient", "Insufficient Funds")

        # --- USER PROFILE & CUSTOMIZATION ---
        self.player_name = self.customization.get("player_name", "Interloper")
        phrase_settings = self.customization.get("phrase_settings", {})
        # --- Inside your Config Class Initialization ---
        self.promptdata_path = self.resolve_json_path(files.get("prompt_data"), root_dir, must_exist=True)
        raw_prompt_json = json.loads(self.promptdata_path.read_text(encoding="utf-8"))
        raw_prompt_json = self._merge_prompt_presets(raw_prompt_json)

        # Structured descriptions for the manager UI
        self.ui_promptdata = raw_prompt_json

        # Flatten the dictionary dynamically in memory for the backend AI engine!
        # This keeps the rest of your text generation loops 100% stable and untouched.
        # Tones/wordiness flatten to prompt strings; per-category context stays intact.
        self.promptdata = build_promptdata(raw_prompt_json)

        # Apply the user's active base prompt variant (Default = the built-in file).
        self.suit_voice_base = self._resolve_base()

        # Keep your existing state trackers exactly as they are
        self.current_tone = phrase_settings.get("tone", "Standard")
        self.current_wordiness = phrase_settings.get("wordiness", "Standard")


        # Audio effect mapping dict for processing pipeline reference
        self.voice_effects = self.customization.get("voice_effects", {})

        # FFMPEG Binary Resolution
        ext_cfg = self.environment.get("external_tools", {})
        ffmpeg_path = shutil.which("ffmpeg")
        if not ffmpeg_path:
            # Fallback config option must resolve to an ABSOLUTE path: a relative
            # entry breaks bare "ffmpeg" subprocess calls when the CWD differs.
            ffmpeg_env = ext_cfg.get("ffmpeg_path") or ext_cfg.get("ffmpeg_cmd")
            if ffmpeg_env:
                ffmpeg_path = Path(ffmpeg_env.strip('"'))
                if not ffmpeg_path.is_absolute():
                    ffmpeg_path = root_dir / ffmpeg_path
                ffmpeg_path = ffmpeg_path.resolve()
                os.environ["PATH"] = str(ffmpeg_path.parent) + ";" + os.environ.get("PATH", "")
            else:
                raise SystemExit("ffmpeg not found on PATH. Please install ffmpeg or set up path contexts.")
        self.ffmpeg_path = Path(ffmpeg_path)

        # --- ML MODEL INITIALIZATIONS ---
        models = self.environment.get("models", {})

        # Pocket TTS settings (engine lazy-loads on first use; see modular/tts_utils.py)
        self.pocket_model_name = models.get("pocket_model", tts_utils.POCKET_DEFAULT_MODEL)
        self.pocket_tts_max_tokens = int(models.get("pocket_tts_max_tokens", 100))
        self.tts_voice = self.customization.get("tts_voice", tts_utils.POCKET_DEFAULT_VOICE)
        self.tts_model = None  # Populated by modular.tts_utils.get_pocket_model()

        # Llama Engine Setup
        self.llm_model = str(self.resolve_json_path(models.get("llm_model"), root_dir, must_exist=True))
        self.llm = None  # You can also use self.llm: Optional[Llama] = None if it squawks here too
        if init_llm:
            self.load_llm()

        self.recent_lines_text = {}  # Runtime session state cache tracker: {wem_id_str: [line1, line2, ...]}

    def load_llm(self):
        """Construct the llama.cpp engine from the configured model (used by the
        pipeline at init and lazily by the manager's combination test)."""
        self.llm = Llama(
            model_path=self.llm_model,
            n_ctx=40960,
            n_batch=1024,
            n_threads=min(os.cpu_count() or 4, 8),  # guard the hardware footprint
            verbose=False,
            flash_attn=True,  # supported by Gemma's architecture; removes V-cache padding
        )
        return self.llm

    def _merge_prompt_presets(self, raw_prompt_json: dict) -> dict:
        """Fold user prompt presets (user/prompt_presets.json) into the built-in sections."""
        try:
            presets = prompt_presets.load_presets(self.root_dir)
        except Exception:
            presets = {}
        self._active_presets = presets
        merged = dict(raw_prompt_json)
        for section in prompt_presets.SECTIONS:
            base = dict(merged.get(section, {}))
            for name, entry in presets.get(section, {}).items():
                if name not in base:  # built-ins stay immutable
                    base[name] = entry
            merged[section] = base
        return merged

    def _resolve_base(self) -> str:
        """Return the active base prompt text (user variant or the built-in file)."""
        presets = getattr(self, "_active_presets", None) or {}
        builtin = getattr(self, "suit_voice_base_builtin", None) or self.suit_voice_base
        return prompt_presets.resolve_base(builtin, presets)

    def settings_mtime(self) -> float:
        """Newest mtime across the live-editable settings files (0.0 if none exist)."""
        candidates = [
            self.user_customization_path,
            self.root_dir / "config" / "environment_config.json",
            prompt_presets.presets_path(self.root_dir),
        ]
        mtimes = [p.stat().st_mtime for p in candidates if p.is_file()]
        return max(mtimes) if mtimes else 0.0

    def reload_user_settings(self) -> list:
        """Re-read personal and runtime settings so manager Apply Changes lands
        without a restart (engine/model selections stay restart-gated)."""
        user = load_json(self.user_customization_path)
        self.user_customization = user
        self.customization = deep_merge(self.default_customization, user)
        self.environment = load_json(self.root_dir / "config" / "environment_config.json")

        changed = []

        def _track(name, old, new):
            if old != new:
                changed.append(name)

        runtime = self.environment.get("runtime", {})
        new_interval = float(runtime.get("check_interval", 0.1))
        _track("check interval", self.check_interval, new_interval)
        self.check_interval = new_interval
        new_cache_max = int(runtime.get("quick_cache_max", 16))
        _track("cache size", self.quick_cache_max, new_cache_max)
        self.quick_cache_max = new_cache_max
        self.max_session_lines = int(runtime.get("max_session_lines", 10))

        paths = self.environment.get("paths", {})
        user_paths = user.get("paths", {})
        mod_dir_setting = user_paths.get("mod_dir") or paths.get("mod_dir")
        new_mod_dir = resolve_mod_wem_dir(mod_dir_setting) if mod_dir_setting else None
        _track("game folder", self.mod_dir, new_mod_dir)
        self.mod_dir = new_mod_dir

        new_name = self.customization.get("player_name", "Interloper")
        _track("player name", self.player_name, new_name)
        self.player_name = new_name

        phrase_settings = self.customization.get("phrase_settings", {})
        new_tone = phrase_settings.get("tone", "Standard")
        _track("tone", self.current_tone, new_tone)
        self.current_tone = new_tone
        new_wordiness = phrase_settings.get("wordiness", "Standard")
        _track("wordiness", self.current_wordiness, new_wordiness)
        self.current_wordiness = new_wordiness

        # Re-merge user prompt presets so manager edits land without a restart.
        try:
            raw_prompt_json = json.loads(self.promptdata_path.read_text(encoding="utf-8"))
            merged_prompt_json = self._merge_prompt_presets(raw_prompt_json)
            _track("prompt presets", self.ui_promptdata, merged_prompt_json)
            self.ui_promptdata = merged_prompt_json
            self.promptdata = build_promptdata(merged_prompt_json)
            # Active base prompt variant (user rewrite or the built-in file).
            new_base = self._resolve_base()
            _track("base prompt", self.suit_voice_base, new_base)
            self.suit_voice_base = new_base
        except Exception:
            pass

        new_effects = self.customization.get("voice_effects", {})
        _track("audio effects", self.voice_effects, new_effects)
        self.voice_effects = new_effects

        new_voice = self.customization.get("tts_voice", tts_utils.POCKET_DEFAULT_VOICE)
        _track("voice", self.tts_voice, new_voice)
        self.tts_voice = new_voice
        self.pocket_tts_max_tokens = int(self.environment.get("models", {}).get("pocket_tts_max_tokens", 100))
        return changed

    def get_tone(self) -> str:
        return self.current_tone or "Standard"

    def get_wordiness(self, category: str) -> str:
        if category in self.mil_cat:
            return "Observer"
        return self.current_wordiness or "Standard"

    def resolve_json_path(self, path_str: str, root: Path, must_exist=True) -> Path:
        if not path_str:
            raise ValueError("Required configuration path entry is missing in JSON targets.")

        p = Path(path_str.strip('"'))
        if not p.is_absolute():
            p = root / p

        if must_exist and not p.exists():
            raise FileNotFoundError(f"Configuration file element targets a missing path reference: {p}")
        return p
