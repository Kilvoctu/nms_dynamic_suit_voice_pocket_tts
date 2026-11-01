# Dynamic No Man's Sky Suit AI Voice Mod Installation

Version: 2.0 (fork)<br>
Original Author: [Bob-Harper](https://github.com/Bob-Harper)<br>
Repository: [nms_dynamic_suit_voice](https://github.com/Bob-Harper/nms_dynamic_suit_voice)


⚠️ Tested on a handful of machines: Windows only, and it wants a decent CPU and RAM. Runs CPU-only by design, the GPU stays with the game.

## Requirements

- Windows 10/11
- Python 3.10 or 3.11 installed and on PATH (3.12+ breaks dependencies)
- Internet access for the one-time downloads (dependencies, ffmpeg, the wav2wem encoder, the local LLM model - roughly 3 GB total)

## Installation

1. Double-click **`run_manager.cmd`**. First run installs the dependencies (a few minutes); until setup completes, only the *System Environment* tab is available.
2. In **System Environment**, enter your No Man's Sky MODS directory (Setup offers to create it):
   - Steam: `C:/Program Files (x86)/Steam/steamapps/common/No Man's Sky/GAMEDATA/MODS/`
   - MS Store: `D:/XboxGames/No Man's Sky/Content/GAMEDATA/MODS/`
3. Press **Setup**. It prepares the venv and dependencies, downloads ffmpeg, the `wav2wem` encoder and the Gemma-4-E2B model into the project folder, copies the 205 starter voice files into your MODS folder, saves the path to your personal config, and verifies the TTS engine end to end.
4. When it reports **Setup Complete**, restart `run_manager.cmd` - all tabs unlock. Re-running Setup later skips completed steps.

## Run the generator

Open **`run_manager.cmd`** (the console flashes and dismisses itself). The manager starts the voice
pipeline automatically and shows live status in the bottom bar. Once it reads **Ready**, start
No Man's Sky. Play until your suit notifies you twice about the same thing; the second phrase
will be different.

Voice: **Voice Model** tab (8 built-ins plus clones, see below). Sound: **Audio Modifiers** tab, where
the current sliders can be saved as your own named presets. Changes apply to the running engine
automatically - no restart needed.

## Voice cloning

Easiest: use the **Clone a Voice** panel in the manager's **Voice Model** tab (pick a name and a
clean sample, press Clone Voice). To do it by hand instead:

1. Get a clean sample: one speaker, no music or background noise, 10-30 seconds.
2. Export the voice state (create folders if they don't exist):
   ```
   venv\Scripts\pocket-tts.exe export-voice C:\path\to\sample.wav user\pocket_voices\myvoice\_model_states\english_2026-10.safetensors --language english_2026-10
   ```
3. The voice appears in the **Voice Model** tab - select it and press **Set as Active Voice**;
   the running engine picks it up automatically.

Clones stay local in `user/pocket_voices/` (gitignored). A voice state must be exported for the
pinned model (`english_2026-10`); states for other model versions won't load. Remove a clone via
**Remove Voice** in the Voice Model tab. If `export-voice` reports "voice cloning unsupported",
log in once with `hf auth login` (free HuggingFace account, accept the model's terms) and retry.

## Shutting it down

Closing the manager window hides it to the tray while the engine runs, and quits outright if the
engine is stopped. The tray icon's **Exit - stops the voice engine** stops the engine (finishing the
current line first, up to a minute) and quits the manager in one step. The bottom bar's **Stop**
stops the engine only, leaving the manager open.

## Uninstall

Remove the project directory and the `DYNAMIC_SUIT_VOICE` folder from the game's MODS directory.
ffmpeg, the wav2wem encoder, the venv and all models live inside the project folder and disappear with it.
