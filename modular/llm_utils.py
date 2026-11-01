import time
import re


def create_logit_bias(config, category_l):
    logit_bias = {
        **extract_token_ids(config.logit_banlist.get(category_l, {})),
        **extract_token_ids(config.logit_banlist.get("Standard", {})),
    }  # to add more categorys like Encouraged or Discouraged with different scores use Default as template
    return logit_bias


def extract_token_ids(data: dict):
    """Flatten token dict and ignore non-integer keys like 'bias'."""
    return {int(k): v for k, v in data.get("tokens", {}).items()}


def reword_phrase(config, wem_id_r,
                  category_r,
                  original_phrase_r,
                  finalprompt):
    # enforce usage or avoidance of specific tokens using logits
    logit_bias_list = create_logit_bias(config, category_r)

    messages = [{"role": "system", "content": finalprompt}]
    max_retries = 3
    for attempt in range(max_retries):
        try:
            # print(f"Raw Input:\n {messages}")
            output = config.llm.create_chat_completion(
                messages=messages,
                max_tokens=512,  # Gemma-4 answers directly (no hidden thinking); 512 caps rambling
                temperature=0.75,
                top_k=90,
                top_p=0.9,
                repeat_penalty=1.25,
                logit_bias=logit_bias_list,
                seed=-1  # must add this to randomize the results
            )
            # print(f"Raw Output:\n {output}")
            result = output["choices"][0]["message"]["content"].strip()
            result = postprocess_for_tts(result)
            add_final_output_line(config, wem_id_r, result)
            # print(f"add_final_output_line:\n {add_final_output_line}")

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

    # Split on sentence-ending punctuation or certain common words, keep trailing whitespace
    split_pattern = r'(?<=[.!?])|(?=\bwith\b)|(?=\band\b)|(?=\bfor\b)|(?=\bof\b)'
    sublines = [s.strip() for s in re.split(split_pattern, line) if s.strip()]

    lines = config.recent_lines_text[wem_id]
    lines.extend(sublines)

    # Trim oldest if over max
    while len(lines) > config.max_session_lines:
        lines.pop(0)

    config.recent_lines_text[wem_id] = lines
