import os
import time
# from debug.log_config import debug_print  # this page works fine


WEM_DIR = r"C:\Program Files (x86)\Steam\steamapps\common\No Man's Sky\GAMEDATA\MODS\DYNAMIC_SUIT_VOICE\AUDIO\WINDOWS\MEDIA\ENGLISH(US)"
now = time.time()

for filename in os.listdir(WEM_DIR):
    if filename.lower().endswith(".wem"):
        path = os.path.join(WEM_DIR, filename)
        try:
            os.utime(path, (now, now))  # (access time, modified time)
        except Exception as e:
            print(f"Failed to update {filename}: {e}")
