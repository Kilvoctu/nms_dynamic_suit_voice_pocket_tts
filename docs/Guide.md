# Dynamic No Man's Sky Suit AI Voice Mod Guide

Version: 2.0 (fork)<br>
Original Author: [Bob-Harper](https://github.com/Bob-Harper)<br>
Repository: [nms_dynamic_suit_voice](https://github.com/Bob-Harper/nms_dynamic_suit_voice)<br>
Fork Repository: [nms_dynamic_suit_voice_pocket_tts](https://github.com/Kilvoctu/nms_dynamic_suit_voice_pocket_tts)


⚠️ Tested on a handful of machines: Windows only, and it wants a decent CPU and RAM. Runs CPU-only by design, the GPU stays with the game.

## Requirements

- Windows 10/11
- Python 3.10 or 3.11 installed and on PATH (3.12+ breaks dependencies)
- Internet access for the one-time downloads (dependencies, ffmpeg, the wav2wem encoder, the local LLM model - roughly 4.5 GB total)

## Installation

1. Double-click **`run_manager.cmd`**. First run installs the dependencies (a few minutes); until setup completes, only the *System Environment* tab is available.
2. In **System Environment**, enter your No Man's Sky MODS directory (Setup offers to create it):
   - Steam (example): `C:/Program Files (x86)/Steam/steamapps/common/No Man's Sky/GAMEDATA/MODS/`
   - MS Store (example): `D:/XboxGames/No Man's Sky/Content/GAMEDATA/MODS/`
3. Press **Setup**. It prepares the venv and dependencies, downloads ffmpeg, the `wav2wem` encoder and the Gemma 4 LLM model(s) (the E2B default, plus the E4B when it is selected in Advanced Settings; ~3-8 GB), copies the starter voice files into your MODS folder, saves the path to your personal config, and verifies the TTS engine end to end.
4. When it reports **Setup Complete**, restart `run_manager.cmd` - all tabs unlock. Re-running Setup later skips completed steps.

## Run the generator

Open **`run_manager.cmd`** (the console flashes and dismisses itself). The manager starts the voice pipeline automatically and shows live status in the bottom bar. Once it reads **Ready**, start No Man's Sky. Play until your suit notifies you twice about the same thing; the second phrase will be different.

Voice: **Voice Model** tab (8 built-ins plus clones, see below). Sound: **Audio Modifiers** tab, where the current sliders can be saved as your own named presets. Changes apply to the running engine automatically - no restart needed. After applying any change, the suit's lines are quietly re-recorded in the background.

## Rephrasing model

**System Environment → Advanced Settings → Rephrasing model** picks which local Gemma 4 model rephrases your lines. **E2B** (default) is light and fast; **E4B** delivers wider phrasing variety and stronger persona flavor at roughly three times the generation time. Swap on **Apply Changes** - if the engine is running it restarts automatically (a few seconds to reload the model). If the selected model file is missing, the engine falls back to E2B until **Setup** downloads it (the First-Time Setup panel reappears in the System tab when that happens). Advanced Settings also holds the cache-depth, worker and CPU-thread knobs.

**System Environment → Troubleshooting → Wipe Cache and Unused Model** erases all cached voice variants and temp renders, **plus** the Gemma 4 GGUF of the model that is *not* selected - handy after a swap to reclaim the disk space. The active model and Pocket TTS weights are never touched; a deleted model re-downloads via **Setup** any time.

## Prompt presets

Tone and verbiage presets live on the **User Profile** tab; built-ins can't be changed. **Manage** opens the preset editor where you can duplicate a built-in or create your own. Your presets are saved in `state/user/prompt_presets.json`.

**Core Prompt** is the suit's core instruction block: duplicate it, edit the copy, **Activate** to select it (committed on **Apply Changes**). It shapes every line the suit speaks. Change with care. **Distill Chat Persona...** rewrites a long chat persona (JSON or TXT) into a compact TTS core prompt with the local model.

**Combination Test** runs any tone + verbiage pairing against a real in-game phrase: pick category and phrase, check the exact prompt, **Generate Test Phrase** to generate with the local model (loads on first use).

## Voice cloning

Use the **Clone a Voice** panel in the manager's **Voice Model** tab: type a name (spaces are fine and become underscores; `\ / : * ? " < > |` aren't allowed), pick a clean sample, press **Clone Voice**. The first clone downloads the cloning model once (online, from Hugging Face); after that, cloning and the voice engine itself stay fully local.

A clean sample means one speaker, no music or background noise, 10-30 seconds, as a `.wav` file. The voice appears in the **Voice Model** tab - select it and press **Apply Changes** to make it your suit's voice (the engine re-renders the mod folder to match). Clones stay local in `user/pocket_voices/`, and a voice state only loads for the pinned model (`english_2026-10`). Remove a clone via **Remove Voice**.

If cloning reports "voice cloning unsupported", press **Sign in & Preload Model** in the Voice Model tab: it opens a Hugging Face sign-in window (paste an access token from huggingface.co - free account, create one under Settings → Access Tokens and accept the model's terms there) and then downloads the cloning model. The sign-in is remembered on your PC, so press **Clone Voice** again and it works from then on.

## Shutting it down

Closing the manager window hides it to the tray while the engine runs, and quits outright if the engine is stopped. The tray icon's **Exit - stops the voice engine** stops the engine (finishing the current line first, up to a minute) and quits the manager in one step. The bottom bar's **Stop** stops the engine only, leaving the manager open.

## Uninstall

Remove the project directory and the `DYNAMIC_SUIT_VOICE` folder from the game's MODS directory. ffmpeg, the wav2wem encoder, the venv and all models live inside the project folder and disappear with it.
