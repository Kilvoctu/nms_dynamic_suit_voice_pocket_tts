import json
from transformers import AutoTokenizer

# Paths
INPUT_JSON = "../data/logit_bias.json"   # merged format file
TOKENIZER_DIR = "../assets/qwen3_06b_q4"

# Load tokenizer
tokenizer = AutoTokenizer.from_pretrained(TOKENIZER_DIR, use_fast=True)

def generate_variants(word: str):
    """Generate common variants for a single word/token."""
    variants = {
        word,
        word.lower(),
        word.capitalize(),
        word.upper(),
        f" {word}",      # leading space
        f"{word} ",      # trailing space
        f" {word} "
    }
    if not word.endswith("'s"):
        variants.update({f"{word}'s", f" {word}'s"})
    if not word.endswith("s"):
        variants.update({f"{word}s", f" {word}s"})

    return variants

# Load JSON
with open(INPUT_JSON, encoding="utf-8") as f:
    categories = json.load(f)

# Process each category
for cat_name, cat_data in categories.items():
    bias_value = cat_data.get("bias", 0)
    human_list = cat_data.get("human_readable", cat_data.get("banned", []))

    token_ids_set = set()
    for phrase in human_list:
        phrase_token_ids = tokenizer.encode(phrase, add_special_tokens=False)
        phrase_tokens = tokenizer.convert_ids_to_tokens(phrase_token_ids)

        for token in phrase_tokens:
            for variant in generate_variants(token):
                token_ids = tokenizer.encode(variant, add_special_tokens=False)
                token_ids_set.update(token_ids)

    # Update JSON structure
    categories[cat_name] = {
        "bias": bias_value,
        "human_readable": human_list,
        "tokens": {str(tid): bias_value for tid in token_ids_set}
    }

# Save back to file
with open(INPUT_JSON, "w", encoding="utf-8") as f:
    json.dump(categories, f, indent=2, ensure_ascii=False)

print("Logits updated with tokenized entries.")

# Helper
def get_logit_bias(category: str) -> dict:
    cat = categories.get(category, categories.get("Standard", {}))
    return {int(tid): bias for tid, bias in cat.get("tokens", {}).items()}
