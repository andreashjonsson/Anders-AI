# Anders – your own AI assistant

Anders is a voice assistant you talk to, like Siri or Alexa, but it runs **entirely on your own computer**.
Nothing is sent to the internet. It understands and speaks both **Swedish and English**.

Just say **"Hej Anders"** or **"Hey Anders"** and ask something:

> "Hej Anders, vad blir det för väder imorgon?"
> "Hey Anders, set a timer for 10 minutes."
> "Hej Anders, lägg till mjölk på inköpslistan."

---

## ✅ Before you start: do you have the right computer?

| You need | Why |
|---|---|
| **Ubuntu Linux** (24.04 or newer) | Anders is built for Linux |
| **An NVIDIA graphics card with at least 8 GB memory** | Runs the brain and the voice. Check with `nvidia-smi` in a terminal |
| **16 GB RAM or more** (32 GB is better) | The AI brain is big |
| **About 30 GB free disk space** | For the AI models |
| **A microphone and speakers** (a headset works best) | So you can talk to it |

> 💡 No NVIDIA card? Anders still works, but slowly, and with a more robotic voice.

---

## 🛠️ Installation (about 30–60 minutes, mostly waiting for downloads)

Open a **terminal** (press `Ctrl + Alt + T`) and copy-paste one step at a time.
When it asks for your password, type it and press Enter. You won't see the letters as you type, which is normal.

### Step 1: Install the basics

```bash
sudo apt update
sudo apt install -y git curl libportaudio2 python3-gi gir1.2-webkit2-4.1 ubuntu-drivers-common
sudo ubuntu-drivers install
```

The last line installs the NVIDIA graphics driver. **Restart the computer** after this step.

### Step 2: Install Ollama and OpenJarvis (Anders' engine)

```bash
curl -fsSL https://ollama.com/install.sh | sh
curl -fsSL https://open-jarvis.github.io/OpenJarvis/install.sh | bash
```

If the installer asks questions, the default answers are fine (just press Enter).
**Close the terminal and open a new one** when it's done.

### Step 3: Download and install Anders

```bash
git clone https://github.com/andreashjonsson/Anders-AI.git
cd Anders-AI
./install.sh
```

This downloads about 20 GB, so grab a coffee ☕. You can run `./install.sh` again if it gets interrupted.

### Step 4: Start Anders 🎉

Open **Anders** from the app menu (press the Super/Windows key and type "Anders"), or type:

```bash
anders-app
```

The **first time** takes a few minutes while the voice loads. Then say **"Hej Anders!"**

---

## 🎙️ Using Anders

- **Talk:** Say "Hej Anders" (Swedish) or "Hey Anders" (English), then your question. Anders replies in the same language.
- **Follow-up:** Right after Anders answers, you have 5 seconds to say more without "Hej Anders" again.
- **Type:** You can also type in the chat box in the window.
- **Settings:** Click the ⚙️ gear icon to set your home town (for weather), voice warmth and more.
- **Terminal mode:** Type `anders` in a terminal to run without a window.
- **Close:** Closing the window turns Anders off completely.

Things Anders can do: weather, timers and alarms, shopping/to-do lists, electricity prices, open apps,
remember things about you, and answer general questions.

---

## 🎨 Make Anders yours

**Use your own voice.** Anders speaks with Andreas' voice by default. To use yours instead, close Anders first, then:

```bash
anders-record-voice
```

Read the text on screen out loud (about 30 seconds) in a quiet room.

**Change the icon** to a photo of yourself:

```bash
anders-set-logo /path/to/photo.jpg
```

**Teach it your name.** In a few places Anders assumes the user is called "Andreas".
Open these files and replace `Andreas` with your name:
`~/.openjarvis/app/anders_memory.py`, `~/.openjarvis/app/anders_tools.py`, `~/.openjarvis/app/static/index.html`.
Then write a bit about yourself in `~/.openjarvis/USER.md`, for example:

```
- Name: Kalle
- Language: Swedish
- Location: Göteborg
```

---

## 🆘 Something's not working?

| Problem | Try this |
|---|---|
| Anders doesn't hear me | Check that the right microphone is selected under **Settings → Sound** in Ubuntu |
| Anders hears me but doesn't answer | Check that Ollama is running: `ollama list` should show `gpt-oss-coder` |
| The voice sounds robotic | The good voice needs the NVIDIA card. Check that `nvidia-smi` works |
| "OpenJarvis is not installed yet" | Do Step 2 again, then open a **new** terminal |
| Very slow answers | Normal on weaker computers. In ⚙️ settings, pick a smaller model (for example `qwen3:8b`, after running `ollama pull qwen3:8b`) |
| Something else | Check the log: `cat ~/.openjarvis/.state/anders-app.log`, and send it to Andreas |

## 🔄 Getting updates

When Andreas improves Anders:

```bash
cd Anders-AI
git pull
./install.sh
```

---

<details>
<summary>🤓 For the curious: how it works</summary>

| Part | Technology |
|---|---|
| Brain (LLM) | `gpt-oss:20b` via [Ollama](https://ollama.com) (`config/gpt-oss-coder.Modelfile`) |
| Hearing (speech-to-text) | KBLab `kb-whisper-small`, Whisper trained on Swedish |
| Voice (text-to-speech) | [Chatterbox](https://github.com/resemble-ai/chatterbox) with voice cloning (GPU), [Piper](https://github.com/rhasspy/piper) as a backup |
| Framework | [OpenJarvis](https://github.com/open-jarvis/OpenJarvis) |
| App | FastAPI backend + web UI in a GTK/WebKit window |

| Folder | Contents |
|---|---|
| `app/` | Backend, tools, memory, alarms, system control, UI |
| `plugins/` | Wake-word listening and the bilingual voice client |
| `chatterbox/` | The local voice server |
| `bin/` | The `anders`, `anders-app`, `anders-record-voice` and `anders-set-logo` commands |
| `config/` | Personality (`SOUL.md`), settings and model config |
| `voices/` | The default voice |
| `desktop/` | App menu entries and icon |

</details>
