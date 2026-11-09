import random
import re


def build_promptdata(raw_prompt_json):
    """Flatten prompt_data.json for the engine: tones become {name: prompt string},
    wordiness keeps its structured entry ({prompt, min_sentences, max_sentences}),
    and per-category context entries are preserved verbatim."""
    promptdata = {}
    for key, val in raw_prompt_json.items():
        if key == "tones":
            promptdata[key] = {
                name: (entry["prompt"] if isinstance(entry, dict) else entry)
                for name, entry in (val or {}).items()
            }
        elif key == "wordiness":
            promptdata[key] = {
                name: {
                    "prompt": (entry.get("prompt", "") if isinstance(entry, dict) else str(entry)),
                    "min_sentences": entry.get("min_sentences") if isinstance(entry, dict) else None,
                    "max_sentences": entry.get("max_sentences") if isinstance(entry, dict) else None,
                }
                for name, entry in (val or {}).items()
            }
        else:
            promptdata[key] = val["prompt"] if isinstance(val, dict) else val
    return promptdata


def length_phrase(min_sentences, max_sentences) -> str:
    """Render a verbiage preset's sentence span as the tail reminder line
    ('' when the preset carries no span - the reminder then simply omits)."""
    try:
        min_s = max(1, int(min_sentences))
        max_s = max(1, int(max_sentences))
    except (TypeError, ValueError):
        return ""
    if max_s < min_s:
        min_s, max_s = max_s, min_s
    if min_s == max_s:
        return f"Response Length: exactly {min_s} sentence{'s' if min_s != 1 else ''}."
    return f"Response Length: {min_s} to {max_s} sentences."


def player_title(config) -> str:
    """The addressee for {name} fills: the set name, or a generic title when blank
    (nameless mode - she must never use or invent any name)."""
    return config.player_name.strip() or "the player"


def name_instruction(config) -> str:
    """The direct-address rule line: named mode allows the vocative; nameless
    mode forbids any name or title outright."""
    name = config.player_name.strip()
    if name:
        return f"Address {name} directly in second person only - never refer to them in third person."
    return ("Address the player directly in second person only - never refer to them in third person, "
            "and never use any name or title for them.")


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
    base = config.suit_voice_base.rstrip()
    if not config.player_name.strip():
        # nameless mode: swap the name-usage policy for a strict no-name rule
        base = re.sub(r'Use the name ".*?" only when directly addressing .*? by name\.',
                      'Never use any name or title; address the player only as "you" or "your".',
                      base)
    system_prompt = base + "\n\n"
    tone = determine_tone(config)
    wordiness_level = determine_wordiness(config)
    category_context = config.promptdata.get(category, config.promptdata.get("Default", ""))
    wordiness_entry = config.promptdata.get("wordiness", {}).get(wordiness_level) or {}
    if isinstance(wordiness_entry, str):  # legacy flattened entry
        wordiness_entry = {"prompt": wordiness_entry}
    wordiness_prompt = wordiness_entry.get("prompt", "")
    length_reminder = length_phrase(
        wordiness_entry.get("min_sentences"), wordiness_entry.get("max_sentences")
    )
    tone_prompt = config.promptdata.get("tones", {}).get(tone, "")
    recent_lines = retrieve_recent_lines(config, wem_id)

    # Assemble dynamic prompt
    system_prompt += config.suit_voice_dynamic.format(
        category_type=category.strip(),
        input_intent=intent.strip(),
        category_context=category_context.strip(),
        wordiness_prompt=wordiness_prompt.strip(),
        tone_prompt=tone_prompt.strip(),
        recent_lines=recent_lines.strip(),
        name_instruction=name_instruction(config),
        length_reminder=length_reminder
    )

    # second pass: any {name} left in the base prompt or corpus renders generically
    return _fill_name(system_prompt, config)

def construct_milcat_prompt(config, category, intent, phrase, wem_id):
    # Base system instructions for combat telemetry
    recent_lines = retrieve_recent_lines(config, wem_id)
    system_prompt = config.suit_voice_combat.format(
        name=player_title(config),
        category_type=category.strip(),
        input_intent=intent.strip(),
        category_context=config.promptdata.get(category, ""),
        recent_lines = recent_lines.strip(),
    )

    # second pass: any {name} left in the category context renders generically
    return _fill_name(system_prompt, config)


def _fill_name(text: str, config) -> str:
    """Second-pass {name} fill for base/corpus leftovers; in nameless mode the
    generic title is capitalized when it lands on a sentence start."""
    if "{name}" not in text:
        return text
    title = player_title(config)
    if title == "the player":
        text = re.sub(r"(?m)(^|[.!?]\s+)\{name\}", lambda m: m.group(1) + "The player", text)
    return text.replace("{name}", title)


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
