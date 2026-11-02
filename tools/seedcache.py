from pathlib import Path
import shutil
# from debug.log_config import debug_print  # this page works fine

mod_dir = Path(r"C:\Program Files (x86)\Steam\steamapps\common\No Man's Sky\GAMEDATA\MODS\DYNAMIC_SUIT_VOICE\AUDIO\WINDOWS\MEDIA\ENGLISH(US)")           # your mod_dir with the original WEMs
cache_base = Path(r"C:/NMS_SUIT_VOICE/cache/tmp_quick_dir")  # base cache directory

for wem_file in mod_dir.glob("*.wem"):
    wem_id = wem_file.stem
    cache_dir = cache_base / wem_id
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Skip if cache already has files
    if any(cache_dir.glob("*.wem")):
        print(f"[SeedCache] Cache for {wem_id} already has files, skipping")
        continue

    # Copy the mod_dir file to cache
    dest = cache_dir / wem_file.name
    shutil.copy2(wem_file, dest)
    print(f"[SeedCache] Copied {wem_file.name} to cache for {wem_id}")
