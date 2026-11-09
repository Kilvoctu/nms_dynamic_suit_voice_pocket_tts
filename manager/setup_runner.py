# manager/setup_runner.py
"""One-click setup pipeline - deliberately stdlib-only (runs before heavy deps)."""
import hashlib
import json
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

from engine.path_utils import (
    LLM_VARIANTS,
    STARTER_WEMS_SUBPATH,
    active_llm_variant,
    assets_root,
    is_frozen,
    project_root,
    resolve_mod_root,
    resolve_mod_wem_dir,
)

PROJECT_ROOT = project_root()
ASSETS_ROOT = assets_root()

WAV2WEM_URL = "https://github.com/pas2k/wav2wem/releases/download/v0.1/wav2wem.exe"
WAV2WEM_SHA256 = "5a10738a73d5451ede6a526a7aa0bfe7f42b41faffe78c9b62ec9f223f91d736"
# The ffmpeg release zip carries everything; only ffplay.exe is used for the tester.
FFPLAY_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-lgpl.zip"
FFPLAY_ZIP_BIN_DIR = "ffmpeg-master-latest-win64-lgpl/bin"


def is_setup_complete(project_root: Path = PROJECT_ROOT) -> bool:
    """Stdlib-only readiness probe: True when the utility can run every tab
    and the game pipeline has everything it needs."""
    pr = Path(project_root)
    if not is_frozen():
        # Source runs still need the Python environment; the frozen exe ships one.
        if not (pr / "venv/Scripts/python.exe").is_file():
            return False
        if not (pr / "venv/Lib/site-packages/pocket_tts").exists():
            return False
    if not (pr / "state/bin/ffplay.exe").is_file():
        return False
    if not (pr / "state/bin/wav2wem.exe").is_file():
        return False
    if not any((pr / "state" / "models").glob("*/*.gguf")):
        return False

    try:
        user_file = (pr / "state" / "user" / "user_customization.json")
        user_data = json.loads(user_file.read_text(encoding="utf-8"))
    except Exception:
        user_data = {}
    # The personal mod dir lives in the untracked user config (Setup registers it).
    mod_dir_setting = user_data.get("paths", {}).get("mod_dir", "")
    if not mod_dir_setting:
        return False
    mod_dir = resolve_mod_wem_dir(mod_dir_setting)
    return mod_dir.is_dir() and any(mod_dir.glob("*.wem"))


class SetupRunner:
    """Executes the full first-time setup. All steps are idempotent: work that
    is already on disk is skipped, so the button is safe to press repeatedly."""

    def __init__(self, project_root: Path, mod_wem_dir: Path, log=print):
        self.project_root = Path(project_root)
        self.mod_wem_dir = Path(mod_wem_dir)
        self.log = log
        self.venv_python = self.project_root / "venv" / "Scripts" / "python.exe"

    # ------------- low-level helpers -------------
    def _download(self, url: str, dest: Path, label: str):
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        req = urllib.request.Request(url, headers=headers)
        started = time.monotonic()
        with urllib.request.urlopen(req) as r, open(dest, "wb") as f:
            total = int(r.headers.get("content-length", 0))
            done = 0
            next_mark = 0
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total and done >= next_mark:
                    self.log(f"  {label}: {done / (1024 * 1024):.0f} / {total / (1024 * 1024):.0f} MB")
                    next_mark += 100 * 1024 * 1024
        self.log(f"  {label} downloaded in {time.monotonic() - started:.1f}s")

    def _venv_python(self) -> str:
        if self.venv_python.is_file():
            return str(self.venv_python)
        return sys.executable

    # ------------- individual steps -------------
    def step_venv_and_deps(self):
        """Source-run only: the frozen exe ships its own bundled interpreter."""
        venv_dir = self.project_root / "venv"
        if not (venv_dir / "Scripts" / "python.exe").is_file():
            self.log("Creating virtual environment (venv/)...")
            subprocess.run([sys.executable, "-m", "venv", "venv"],
                           cwd=self.project_root, check=True)
        else:
            self.log("Virtual environment already present, skipping creation.")

        marker = venv_dir / "Lib" / "site-packages" / "pocket_tts"
        if marker.exists():
            self.log("Python dependencies already installed, skipping pip.")
            return
        self.log("Installing Python dependencies (pocket-tts pulls torch; this can take a few minutes)...")
        started = time.monotonic()
        result = subprocess.run(
            [str(self.venv_python), "-m", "pip", "install", "--prefer-binary", "--only-binary=llama-cpp-python", "-r", "requirements.txt"],
            cwd=self.project_root, capture_output=True, text=True,
            creationflags=0x08000000 if sys.platform == "win32" else 0,
        )
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "")[-1500:]
            raise RuntimeError(f"pip install failed:\n{tail}")
        self.log(f"  dependencies installed in {time.monotonic() - started:.1f}s")

    def step_tools_from_bundle(self):
        """Frozen runs ship the tools inside the bundle: seed copies into state/bin
        so the user folder owns every tool (idempotent, one-time)."""
        if not is_frozen():
            return
        bin_dir = self.project_root / "state" / "bin"
        for name in ("ffplay.exe", "wav2wem.exe"):
            dest = bin_dir / name
            if dest.is_file():
                self.log(f"  {name} already present in state/bin.")
                continue
            bundled = assets_root() / "bin" / name
            if bundled.is_file():
                bin_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(bundled, dest)
                self.log(f"  {name} seeded into state/bin from the bundle.")

    def step_ffplay(self):
        ffplay_exe = self.project_root / "state" / "bin" / "ffplay.exe"
        if ffplay_exe.is_file():
            self.log("ffplay already present, skipping download.")
            return
        self.log("Downloading ffplay (playback tester; zip carries ffmpeg too, only ffplay is kept)...")
        zip_path = self.project_root / "ffplay-temp.zip"
        extract_dir = self.project_root / "ffplay-temp"
        try:
            self._download(FFPLAY_URL, zip_path, "ffplay")
            with zipfile.ZipFile(zip_path) as z:
                z.extractall(extract_dir)
            bin_dir = extract_dir / FFPLAY_ZIP_BIN_DIR
            tools_dir = self.project_root / "state" / "bin"
            tools_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bin_dir / "ffplay.exe", tools_dir / "ffplay.exe")
        finally:
            if zip_path.exists():
                zip_path.unlink()
            if extract_dir.exists():
                shutil.rmtree(extract_dir)
        if not ffplay_exe.is_file():
            raise RuntimeError("ffplay.exe not found after extraction.")
        self.log("  ffplay ready.")

    def step_wav2wem(self):
        exe = self.project_root / "state" / "bin" / "wav2wem.exe"
        if exe.is_file():
            self.log("wav2wem encoder already present, skipping download.")
            return
        self.log("Downloading wav2wem.exe (standalone WAV to WEM encoder)...")
        exe.parent.mkdir(parents=True, exist_ok=True)
        self._download(WAV2WEM_URL, exe, "wav2wem")
        got = hashlib.sha256(exe.read_bytes()).hexdigest()
        if got.lower() != WAV2WEM_SHA256:
            exe.unlink()
            raise RuntimeError(f"wav2wem.exe checksum mismatch (got {got}). File deleted; run Setup again.")
        self.log("  wav2wem.exe checksum OK.")

    def step_model(self):
        """Default E2B always materializes (the engine's fallback); the selected
        variant is fetched too so the swap in Advanced Settings can land."""
        self._ensure_llm_file("e2b", "LLM rephrasing model")
        variant = self._selected_llm_variant()
        if variant in LLM_VARIANTS and variant != "e2b":
            self._ensure_llm_file(variant, f"selected LLM variant ({variant})")

    def _selected_llm_variant(self) -> str:
        user_file = self.project_root / "state" / "user" / "user_customization.json"
        try:
            user_cfg = json.loads(user_file.read_text(encoding="utf-8"))
        except Exception:
            user_cfg = {}
        return active_llm_variant({}, user_cfg)

    def _ensure_llm_file(self, variant_key: str, label: str):
        meta = LLM_VARIANTS[variant_key]
        target = self.project_root / meta["dir"] / meta["file"]
        if target.is_file() and target.stat().st_size > 10 * 1024 * 1024:
            self.log(f"{label} already present, skipping download.")
            return
        self.log(f"Downloading {label} ({meta['file']}, several GB)...")
        target.parent.mkdir(parents=True, exist_ok=True)
        self._download(meta["url"], target, "llm model")
        self.log(f"  {label} ready.")

    def step_starter_wems(self):
        starter = assets_root() / STARTER_WEMS_SUBPATH  # bundled copy when frozen
        self.mod_wem_dir.mkdir(parents=True, exist_ok=True)
        copied = 0
        total = 0
        for wem in starter.glob("*.*"):
            total += 1
            dest = self.mod_wem_dir / wem.name
            if not dest.exists():
                shutil.copy(wem, dest)
                copied += 1
        self.log(f"  starter voice files: {copied} copied, {total - copied} already present ({total} total).")
        if total == 0:
            raise RuntimeError(f"No starter WEMs found in {starter}")

    def step_register_mod_dir(self):
        user_file = self.project_root / "state" / "user" / "user_customization.json"
        user_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            user_data = json.loads(user_file.read_text(encoding="utf-8"))
        except Exception:
            user_data = {}
        registered = str(resolve_mod_root(self.mod_wem_dir)).replace("\\", "/")
        user_data.setdefault("paths", {})["mod_dir"] = registered
        user_file.write_text(json.dumps(user_data, indent=4), encoding="utf-8")
        self.log(f"  registered mod directory: {registered}")

    def step_warmup(self):
        self.log("Warming up the TTS engine and ALL voices (downloads per-voice assets on first run; may take a while)...")
        if is_frozen():
            # In-process warmup: the exe already bundles the runtime and engine.
            import io
            from contextlib import redirect_stdout

            from engine import warmup_tts

            sink = io.StringIO()
            try:
                with redirect_stdout(sink):
                    warmup_tts.main()
            except SystemExit as code:
                if code.code not in (0, None):
                    raise RuntimeError("TTS warmup failed (see state/cache/manager_stdout.log).") from None
            for line in sink.getvalue().splitlines()[-12:]:
                self.log(f"  {line}")
            self.log("  TTS engine warm and verified end-to-end.")
            return
        result = subprocess.run(
            [self._venv_python(), "engine/warmup_tts.py"],
            cwd=self.project_root, capture_output=True, text=True,
            creationflags=0x08000000 if sys.platform == "win32" else 0,
        )
        for line in (result.stdout or "").splitlines()[-12:]:
            self.log(f"  {line}")
        if result.returncode != 0:
            tail = (result.stderr or "")[-800:]
            raise RuntimeError(f"TTS warmup failed:\n{tail}")
        self.log("  TTS engine warm and verified end-to-end.")

    # ------------- orchestrator -------------
    def run(self):
        steps = []
        if not is_frozen():
            steps.append(("Python environment", self.step_venv_and_deps))
        steps += [
            ("Bundled tool seeding", self.step_tools_from_bundle),
            ("wave encoder", self.step_wav2wem),
            ("ffplay player", self.step_ffplay),
            ("LLM rephrasing model", self.step_model),
            ("Starter voice files", self.step_starter_wems),
            ("Register mod directory", self.step_register_mod_dir),
            ("TTS engine warmup", self.step_warmup),
        ]
        started = time.monotonic()
        for name, step in steps:
            self.log(f"[Setup] {name}...")
            step()
        self.log(f"[Setup] COMPLETE in {time.monotonic() - started:.1f}s. Restart the utility to unlock all tabs.")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if not target:
        print("Usage: python manager/setup_runner.py <mod directory>")
        sys.exit(1)
    SetupRunner(PROJECT_ROOT, resolve_mod_wem_dir(target)).run()
