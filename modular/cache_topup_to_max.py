# cache_topup_to_max.py
from modular.config import SuitVoiceConfig
from modular.quick_cache import _replacement_wrapper

config = SuitVoiceConfig()

# Every line the game has actually used, from the JSON transcript map.
wem_ids_raw = [
    str(wem_id)
    for wem_id, entry in config.intent_map.items()
    if str(entry.get("Used", "")).strip().lower() == "yes"
]


def fill_cache_for_ids(wem_ids, max_count=None, progress=None):
    """Fill each line's variant cache to max_count. progress(index, total,
    wem_id, missing_count) is called once per line before its renders start."""
    if max_count is None:
        max_count = config.quick_cache_max
    wem_ids = [str(w) for w in wem_ids]
    total_ids = len(wem_ids)

    for idx, wem_id in enumerate(wem_ids, start=1):
        wem_id = str(wem_id)  # force to string

        wem_dir = config.quick_cache_dir / str(wem_id)
        wem_dir.mkdir(parents=True, exist_ok=True)

        existing_files = list(wem_dir.glob("*.wem"))
        existing_count = len(existing_files)

        missing_count = max_count - existing_count
        if missing_count <= 0:
            if progress is not None:
                progress(idx, total_ids, wem_id, 0)
            continue

        if progress is not None:
            progress(idx, total_ids, wem_id, missing_count)

        for i in range(missing_count):
            _replacement_wrapper(config, wem_id, wem_dir)



if __name__ == "__main__":
    fill_cache_for_ids(wem_ids_raw)
