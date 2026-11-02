import csv
import json
from pathlib import Path

# Explicit project path mapping
csv_path = Path("C:\\NMS_SUIT_VOICE\\data\\nms_suit_voice_transcripts.csv")
json_path = Path("C:\\NMS_SUIT_VOICE\\data\\nms_suit_voice_transcripts.json")

def convert_csv_to_json():
    if not csv_path.exists():
        print(f"Error: Could not find source file at {csv_path}")
        return

    print(f"Reading tracking matrix from {csv_path}...")
    json_data = {}

    # utf-8-sig automatically strips out hidden Excel byte-order markers (BOMs)
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        # csv.DictReader automatically respects multiline quotes safely
        reader = csv.DictReader(f)

        for row in reader:
            # Extract and clean up our primary lookup key
            wem_id = row.get("WEM_number", "").strip()

            if not wem_id:
                continue  # Skip any empty data frames or stray newlines

            # Build the nested profile layer
            json_data[wem_id] = {
                "Filename": row.get("Filename", "").strip(),
                "Transcription": row.get("Transcription", "").strip(),
                "Category": row.get("Category", "").strip(),
                "Intent": row.get("Intent", "").strip(),
                "Used": row.get("Used", "").strip()
            }

    # Write out the clean, structured database payload
    print(f"Writing structured layout database to {json_path}...")
    json_path.parent.mkdir(parents=True, exist_ok=True)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_data, f, indent=4, ensure_ascii=False)

    print("Migration complete successfully!")

if __name__ == "__main__":
    convert_csv_to_json()
