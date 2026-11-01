# manager/setup_runner.py
"""One-click setup pipeline - deliberately stdlib-only (runs before the venv exists)."""
import hashlib
import json
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.absolute()

# Shared resolver (stdlib-only module - safe to import before deps exist)
sys.path.insert(0, str(PROJECT_ROOT))
from modular.path_utils import WEM_SUBPATH, resolve_mod_root, resolve_mod_wem_dir  # noqa: E402

WAV2WEM_URL = "https://github.com/pas2k/wav2wem/releases/download/v0.1/wav2wem.exe"
WAV2WEM_SHA256 = "5a10738a73d5451ede6a526a7aa0bfe7f42b41faffe78c9b62ec9f223f91d736"
FFMPEG_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-lgpl.zip"
FFMPEG_ZIP_BIN_DIR = "ffmpeg-master-latest-win64-lgpl/bin"
GEMMA_URL = "https://huggingface.co/unsloth/gemma-4-E2B-it-GGUF/resolve/main/gemma-4-E2B-it-Q4_K_M.gguf"
GEMMA_RELATIVE_DIR = "assets/gemma4_e2b"
GEMMA_FILENAME = "gemma-4-E2B-it-Q4_K_M.gguf"
STARTER_WEMS_RELATIVE = WEM_SUBPATH


def is_setup_complete(project_root: Path = PROJECT_ROOT) -> bool:
    """Stdlib-only readiness probe: True when the utility can run every tab
    and the game pipeline has everything it needs."""
    pr = Path(project_root)
    if not (pr / "venv/Scripts/python.exe").is_file():
        return False
    if not (pr / "venv/Lib/site-packages/pocket_tts").exists():
        return False
    if not (pr / "tools/ffmpeg.exe").is_file():
        return False
    if not (pr / "tools/wav2wem.exe").is_file():
        return False
    if not any((pr / "assets").glob("*/*.gguf")):
        return False

    try:
        env_data = json.loads((pr / "config" / "environment_config.json").read_text(encoding="utf-8"))
    except Exception:
        env_data = {}
    try:
        user_file = (pr / "user" / "user_customization.json")
        if not user_file.is_file():
            user_file = pr / "config" / "user_customization.json"  # legacy location
        user_data = json.loads(user_file.read_text(encoding="utf-8"))
    except Exception:
        user_data = {}
    # Personal mod dir lives in the untracked user config; the env value is
    # only the shipped default / legacy fallback.
    mod_dir_setting = (user_data.get("paths", {}).get("mod_dir")
                       or env_data.get("paths", {}).get("mod_dir", ""))
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
            [str(self.venv_python), "-m", "pip", "install", "--prefer-binary", "-r", "requirements.txt"],
            cwd=self.project_root, capture_output=True, text=True,
            creationflags=0x08000000 if sys.platform == "win32" else 0,
        )
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "")[-1500:]
            raise RuntimeError(f"pip install failed:\n{tail}")
        self.log(f"  dependencies installed in {time.monotonic() - started:.1f}s")

    def step_ffmpeg(self):
        ffmpeg_exe = self.project_root / "tools" / "ffmpeg.exe"
        if ffmpeg_exe.is_file():
            self.log("ffmpeg already present, skipping download.")
            return
        self.log("Downloading ffmpeg (static build, ffmpeg + ffplay, approx 260 MB)...")
        zip_path = self.project_root / "ffmpeg-temp.zip"
        extract_dir = self.project_root / "ffmpeg-temp"
        try:
            self._download(FFMPEG_URL, zip_path, "ffmpeg")
            with zipfile.ZipFile(zip_path) as z:
                z.extractall(extract_dir)
            bin_dir = extract_dir / FFMPEG_ZIP_BIN_DIR
            tools_dir = self.project_root / "tools"
            tools_dir.mkdir(exist_ok=True)
            for name in ("ffmpeg.exe", "ffplay.exe"):
                shutil.copy2(bin_dir / name, tools_dir / name)
        finally:
            if zip_path.exists():
                zip_path.unlink()
            if extract_dir.exists():
                shutil.rmtree(extract_dir)
        if not ffmpeg_exe.is_file():
            raise RuntimeError("ffmpeg.exe not found after extraction.")
        self.log("  ffmpeg ready.")

    def step_wav2wem(self):
        exe = self.project_root / "tools" / "wav2wem.exe"
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
        target = self.project_root / GEMMA_RELATIVE_DIR / GEMMA_FILENAME
        if target.is_file() and target.stat().st_size > 10 * 1024 * 1024:
            self.log("LLM rephrasing model already present, skipping download.")
            return
        env_file = self.project_root / "config" / "environment_config.json"
        try:
            env_data = json.loads(env_file.read_text(encoding="utf-8"))
            url = env_data.get("model_url", {}).get("gemma4_e2b", GEMMA_URL)
        except Exception:
            url = GEMMA_URL
        self.log(f"Downloading LLM rephrasing model ({GEMMA_FILENAME}, approx 3 GB)...")
        target.parent.mkdir(parents=True, exist_ok=True)
        self._download(url, target, "llm model")
        self.log("  LLM model ready.")

    def step_starter_wems(self):
        starter = self.project_root / STARTER_WEMS_RELATIVE
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
        user_file = self.project_root / "user" / "user_customization.json"
        if not user_file.is_file():
            legacy = self.project_root / "config" / "user_customization.json"
            if legacy.is_file():
                user_file.parent.mkdir(parents=True, exist_ok=True)
                legacy.replace(user_file)
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
        result = subprocess.run(
            [self._venv_python(), "modular/warmup_tts.py"],
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
        steps = [
            ("Python environment", self.step_venv_and_deps),
            ("ffmpeg", self.step_ffmpeg),
            ("wav2wem encoder", self.step_wav2wem),
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
