import time
import re


def reword_phrase(config, wem_id_r,
                  category_r,
                  original_phrase_r,
                  finalprompt):
    messages = [{"role": "system", "content": finalprompt}]
    max_retries = 3
    for attempt in range(max_retries):
        try:
            output = config.llm.create_chat_completion(
                messages=messages,
                max_tokens=512,
                temperature=1.0,
                top_p=0.95,
                top_k=100,
                repeat_penalty=1.25,
            )
            result = output["choices"][0]["message"]["content"].strip()
            result = postprocess_for_tts(result)
            add_final_output_line(config, wem_id_r, result)

            return result

        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(1)
            else:
                print(f"LLM ERROR on WEM {wem_id_r}: {e}")
                return f"WEM ERROR {wem_id_r}, {e}. {original_phrase_r}"
    print(f"ERROR on WEM {wem_id_r}")
    return f"External Reality Failure. {original_phrase_r}"


def postprocess_for_tts(text: str) -> str:
    # Strip all non-ASCII characters first
    text = text.encode("ascii", "ignore").decode()
    # Existing cleanup
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)  # Strip Thinking
    text = re.sub(r"<think>\s*", "", text)  # Strip rogue opening think tags with no matching close
    text = re.sub(r'\bfunds\b', 'Units', text, flags=re.IGNORECASE)  # In-Game Currency for immersion
    text = re.sub(r"[—–]", ", ", text)  # convert em-dash / en-dash
    text = re.sub(r"\(es\)", "es", text)  # fix pluralization artifact
    text = re.sub(r"-ing", "ing", text)  # remove hyphenation from suffix

    # --- Kernel-safe additions, solves Kernel Mismatch errors---
    text = text.rstrip("*")              # remove trailing asterisks
    text = text.replace("\n*", "\n")    # fix newline + asterisk
    text = text.replace("\r*", "\r")

    text = text.strip()                  # final strip of whitespace
    if not re.search(r"[.!?]$", text):  # add a period if not ended with sentence-ending punctuation
        text += "."

    return text


def add_final_output_line(config, wem_id, line):
    """Add a new line (or sublines) to the recent lines list for a WEM ID, trimming if necessary."""
    if wem_id not in config.recent_lines_text:
        config.recent_lines_text[wem_id] = []

    # Split on sentence-ending punctuation, semicolons/colons, or certain common words, keep trailing whitespace
    split_pattern = (r'(?<=[.!?;:])|(?=\bwith\b)|(?=\band\b)|(?=\bfor\b)'
                     r'|(?=\bof\b)|(?=\bbut\b)|(?=\byet\b)')
    sublines = [s.strip() for s in re.split(split_pattern, line) if s.strip()]

    lines = config.recent_lines_text[wem_id]
    for subline in sublines:
        if subline not in lines:
            lines.append(subline)

    # Trim oldest if over max
    while len(lines) > config.max_session_lines:
        lines.pop(0)

    config.recent_lines_text[wem_id] = lines
