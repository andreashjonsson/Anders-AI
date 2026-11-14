# Anders – lokal AI-assistent / local AI assistant

Anders is a bilingual (Swedish/English) voice assistant that runs entirely on your own computer.
Say **"Hej Anders"** (svenska) or **"Hey Anders"** (English) and talk to it.

Built on top of [OpenJarvis](https://github.com/open-jarvis/OpenJarvis) with:

- **LLM** via Ollama (default model `gpt-oss-coder`)
- **Speech-to-text:** KBLab `kb-whisper-small` (Swedish-tuned Whisper)
- **Text-to-speech:** Chatterbox (natural, GPU, voice cloning), with Piper as a CPU fallback
- **Desktop app** (FastAPI backend + web UI window) and a terminal mode
- Memory, alarms/timers, lists, weather, electricity prices, launching apps, and other tools

## Repo layout

| Path | What |
|---|---|
| `app/` | Desktop app: backend (`anders_app.py`), tools, memory, alarms, system control, UI (`static/index.html`), window |
| `plugins/` | Hands-free wake-word listening and the bilingual TTS client |
| `chatterbox/server.py` | Local Chatterbox TTS server (GPU) |
| `bin/` | Launchers: `anders`, `anders-app`, `anders-record-voice`, `anders-set-logo` |
| `config/` | Persona (`SOUL.md`), example OpenJarvis config, and example app settings |
| `desktop/` | App-menu entries and icon |
| `voices/reference.wav` | Anders' voice (cloned from Andreas). `install.sh` uses it unless you've recorded your own. |

## Requirements

- Linux (tested on Ubuntu/GNOME), Python 3.13
- An NVIDIA GPU with about 8 GB VRAM for Chatterbox (Piper works on CPU)
- [Ollama](https://ollama.com) with the model pulled: `ollama pull gpt-oss-coder` (or change `model` in settings)
- OpenJarvis installed in `~/.openjarvis` (with its `.venv`)
- A Chatterbox virtualenv in `~/.openjarvis/chatterbox/.venv` (`pip install chatterbox-tts fastapi uvicorn`)

## Install

```bash
git clone <this repo> anders-ai
cd anders-ai
./install.sh
anders-app
```

Then optionally:

- `anders-record-voice`: record ~30 s of your own voice to replace the default one
- `anders-set-logo [image]`: turn a photo into the round app icon

## Make it yours

Anders currently refers to the user as "Andreas" in a few prompts
(`app/anders_memory.py`, `app/anders_tools.py`, `app/static/index.html`, `bin/anders-record-voice`).
Search for `Andreas` and replace it with your own name. The persona lives in `~/.openjarvis/SOUL.md`.
