import random


def build_promptdata(raw_prompt_json):
    """Flatten prompt_data.json for the engine: tones/wordiness become {name:
    prompt string}, while per-category context entries are preserved verbatim."""
    promptdata = {}
    for key, val in raw_prompt_json.items():
        if key in ("tones", "wordiness"):
            promptdata[key] = {
                name: (entry["prompt"] if isinstance(entry, dict) else entry)
                for name, entry in (val or {}).items()
            }
        else:
            promptdata[key] = val["prompt"] if isinstance(val, dict) else val
    return promptdata


def build_suit_prompt(config, category, intent, phrase, wem_id):
    if category in config.mil_cat:  # requires special prompting for best results
        system_prompt = construct_milcat_prompt(config, category, intent, phrase, wem_id)

    # elif category in config.some_other_cat:  # Example for futureproofing if we add more category handling
    #     system_prompt =  construct_other_prompt(config, category, intent, phrase, wem_id)

    else:  # default for all categories not explicitly handled
        system_prompt = construct_standard_prompt(config, category, intent, phrase, wem_id)

    system_prompt = system_prompt.encode("ascii", "ignore").decode()
    return system_prompt

def construct_standard_prompt(config, category, intent, phrase, wem_id):
    # Retrieve prompt components. The base text is joined with a normalized
    # blank-line seam so custom base prompts always combine cleanly.
    system_prompt = config.suit_voice_base.rstrip() + "\n\n"
    tone = determine_tone(config)
    wordiness_level = determine_wordiness(config)
    category_context = config.promptdata.get(category, config.promptdata.get("Default", ""))
    wordiness_prompt = config.promptdata.get("wordiness", {}).get(wordiness_level, "")
    tone_prompt = config.promptdata.get("tones", {}).get(tone, "")
    recent_lines = retrieve_recent_lines(config, wem_id)

    # Assemble dynamic prompt
    system_prompt += config.suit_voice_dynamic.format(
        category_type=category.strip(),
        input_intent=intent.strip(),
        input_phrase=phrase.strip(),
        category_context=category_context.strip(),
        wordiness_prompt=wordiness_prompt.strip(),
        tone_prompt=tone_prompt.strip(),
        recent_lines=recent_lines.strip(),
        name = config.player_name.strip()
    )

    system_prompt = system_prompt.format(name=config.player_name.strip())
    return system_prompt

def construct_milcat_prompt(config, category, intent, phrase, wem_id):
    # Base system instructions for combat telemetry
    recent_lines = retrieve_recent_lines(config, wem_id)
    system_prompt = config.suit_voice_combat.format(
        name=config.player_name.strip(),
        category_type=category.strip(),
        input_intent=intent.strip(),
        input_phrase=phrase.strip(),
        category_context=config.promptdata.get(category, ""),
        recent_lines = recent_lines.strip(),
    )


    return system_prompt


def determine_tone(config):
    tones = list(config.promptdata.get("tones", {}).keys())
    current_tone = config.current_tone

    # Test harnesses lock the selection to preview the exact chosen combination
    if getattr(config, "selection_locked", False):
        return current_tone

    # 90% chance stick with default
    if random.random() < 0.9 or not tones:
        return current_tone

    # 10% chance pick another from the configured tones
    return random.choice([t for t in tones if t != current_tone])


def determine_wordiness(config):
    current = config.current_wordiness

    # Test harnesses lock the selection to preview the exact chosen combination
    if getattr(config, "selection_locked", False):
        return current

    # If observer is explicitly set, never override it
    if current == "Observer":
        return current

    # 95% chance stick with whatever's configured
    if random.random() < 0.95:
        return current

    # 5% chance: override with verbose
    return "Verbose"

def retrieve_recent_lines(config, wem_number):
    """Return recent lines for this WEM ID as a single string, ready for prompt insertion."""
    lines = config.recent_lines_text.get(wem_number, [])
    return "\n".join(lines) if lines else ""
