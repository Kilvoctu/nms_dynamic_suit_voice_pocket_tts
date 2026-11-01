import json
from pathlib import Path

# The absolute source of reality from your folder scan
VERIFIED_35_IDS = {
    "247183138", "1032826641", "330331555", "792296351", "980411923",
    "647699088", "985789170", "774834164", "386328035", "951631813",
    "884631197", "330757140", "968718536", "105331281", "245522736",
    "757274367", "583871976", "906332741", "682086836", "656990144",
    "673620140", "43830316", "240393094", "1023688906", "696022064",
    "797567920", "78601477", "570428671", "976899439", "943635243",
    "486861530", "711767177", "54980060", "383184645", "236472322"
}

json_path = Path("data/nms_suit_voice_transcripts.json")

def total_dehydration():
    if not json_path.exists():
        print("Error: JSON database file not found.")
        return

    with open(json_path, "r", encoding="utf-8") as f:
        master_data = json.load(f)

    lean_data = {}

    # Filter through and ONLY keep what's on the verified list
    for wem_id, details in master_data.items():
        id_str = str(wem_id).strip()
        if id_str in VERIFIED_35_IDS:
            # Force the marker to 'Yes' just in case any were missed
            details["Used"] = "Yes"
            lean_data[id_str] = details

    print(f"Purging complete!")
    print(f"Retained exactly {len(lean_data)} active tracks.")
    print(f"Nuked {len(master_data) - len(lean_data)} obsolete ghost records.")

    # Lock it down to disk
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(lean_data, f, indent=4, ensure_ascii=False)

if __name__ == "__main__":
    total_dehydration()
