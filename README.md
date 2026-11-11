<p align="center">
  <a href="assets/preview.png"><img src="assets/preview.png" width="700" alt="The DSV Manager: User Profile tab"></a>
</p>
<p align="center">
  <a href="assets/preview2.png"><img src="assets/preview2.png" width="700" alt="The DSV Manager: Voice Modifier tab"></a>
</p>

### Requirements
- Windows 10/11
- Internet access for the one-time downloads (~4.5 GB total)

### Installation
1. Download the latest **`NMS_DSV-<version>-win64.zip`** from [Releases](../../releases), unzip it anywhere, and run **`DSV.exe`** in the `NMS_DSV` folder (no install; keep the `DSV.exe` + `DSV_runtime` folder together). Everything the app writes goes into its own `state\` folder beside the exe.
2. In **System Environment**, enter your No Man's Sky MODS directory (Setup offers to create it):
   - Steam (example): `C:/Program Files (x86)/Steam/steamapps/common/No Man's Sky/GAMEDATA/MODS/`
   - MS Store (example): `D:/XboxGames/No Man's Sky/Content/GAMEDATA/MODS/`
3. Press **Setup**. It fetches the voice model into `state\` and copies the mod files to your mods directory.
4. When it reports **Setup Complete**, close and restart the app, and all tabs unlock.

**Run from source instead?** You need Python 3.11-3.13 (64-bit). In the repo root:

   ```bat
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   python main.py
   ```

   Then proceed with steps 2-4 above (MODS directory, **Setup**, restart).
   Once Setup is complete, you can optionally build your own exe: run `build_exe.bat` (it needs the tools Setup already 
   put in `state\bin\`, and produces the same `DSV.exe` + `DSV_runtime` layout as the release zip in the `dist\NMS_DSV` folder).

## Features
- Uses an LLM to rephrase No Man's Sky's canned suit lines.
- A TTS speaks the lines, processed through an EQ + effects chain.
- 8 built-in voices, plus voice cloning support.
- Line variants swap into NMS' MODS folder while the game runs.
- Keeps several variants and restocks biased toward the frequently heard ones.
- Newly heard lines are auto-learned and managed from then on.
- DAW-style Voice Modifier: draggable parametric EQ nodes, an effects rack, and speed/pitch/delivery controls.
- Preset management for both the voice sound and the LLM phrasing (tone/verbiage, with custom prompt support).
- Applying settings quietly re-records the affected lines in the background.
- Fully local and CPU-first: no cloud, no paid APIs; one-time Hugging Face sign-in for voice cloning only.

## Usage
The manager starts the voice engine automatically and shows live status in the bottom bar. Once it says **Ready**, you can
start No Man's Sky, and it will continue to generate variant lines in the background.

### User Profile
In this tab, you can enter your name and select the presets for rephrasing. Generated lines will use that name.
**Manage** opens the preset editor where you can duplicate a built-in or create your own. In there, **Combination Test** 
lets you test how any combination generates a real in-game phrase. If editing the **Core Prompt**, you can use 
**Distill Chat Persona...** and drop in a long chat persona such as a character card (JSON or TXT).
It'll create a summarized prompt to help get you started.

### Voice Modifier
This tab contains all the options for customizing voice; it includes settings for the voice model, a parametric EQ, 
an effects rack, buttons for testing. You can use the built-in presets or make and manage your own.

### Voice Cloning
Located in the **System Environment** tab. It uses a different Pocket TTS model, and you have to accept Kyutai's 
(the developer) terms before cloning. Press **Sign in & Preload Model** to open a Hugging Face sign-in window. When you 
accept, it'll give an access token to paste in then it'll download the cloning model. The rest should be pretty 
self-explanatory. Just ensure to use a clean sample: one speaker, no music or background noise, 10-30 seconds. 
wav, mp3, flac and ogg are all supported.

### Language Model
The default model is Gemma-4 E2B. If you have resources to spare, you can go to **Rephrasing model** under the 
**System Environment** tab and select the **E4B** model, which delivers wider variety and stronger flavor at ~3x the generation time. 
Swap on **Apply Changes** (generally this applies to any change you make); it'll download the model if needed.

### Miscellaneous
**Wipe Cache and Unused Model** erases cached voice variants, temp renders, and the Gemma 4 model that is *not* selected; useful to reclaim disk space.

Closing the manager window keeps the engine running. To close both, stop the engine first, then close the manager, or go 
the tray icon and select **Exit - stops the voice engine**, and it'll cleanly shut down the engine and manager.

Hover over anything in the manager for a tooltip, or just ask in [discussions](https://github.com/Kilvoctu/nms_dynamic_suit_voice_pocket_tts/discussions) and I'll be happy to answer any questions.
This is a personal fork so I was not too concerned about documentation.

### Uninstall
Delete the app folder and the `DYNAMIC_SUIT_VOICE` folder from the game's MODS directory; `state\` keeps your settings; 
back it up or delete it too. Nothing is installed anywhere else. Source installs: remove the project directory the same way.


## Credits & license
Original mod by [Bob-Harper](https://github.com/Bob-Harper) ([nms_dynamic_suit_voice](https://github.com/Bob-Harper/nms_dynamic_suit_voice)).<br>
LLM [gemma-4-E2B-it-GGUF](https://huggingface.co/unsloth/gemma-4-E2B-it-GGUF) running in [llama-cpp-python](https://github.com/abetlen/llama-cpp-python), TTS voice [Pocket TTS](https://github.com/kyutai-labs/pocket-tts), processed and packed with [wav2wem](https://github.com/pas2k/wav2wem). The manager is built on [PySide6](https://code.qt.io/cgit/pyside/pyside-setup.git/).

MIT License, Copyright © 2025-2026 Bob Harper - see [LICENSE](LICENSE).
