# main.py
"""Single entry point: no arguments starts the manager GUI, "engine" or "--managed"
starts the voice engine (tray or headless for the manager respectively)."""
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))


def main():
    args = sys.argv[1:]
    if args and args[0] in ("engine", "--managed"):
        from engine.launcher import run as engine_run
        engine_run()
    else:
        # manager.ui modules import each other via the manager/ package dir.
        sys.path.insert(0, str(ROOT / "manager"))
        from manager.main import main as manager_main
        manager_main()


if __name__ == "__main__":
    main()
