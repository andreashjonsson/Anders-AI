# Anders – Andreas' AI assistant

Anders is a voice assistant you talk to, like Siri or Alexa, but it runs **entirely on your own computer**.
Nothing is sent to the internet. It understands and speaks both **Swedish and English**, with Andreas' voice.

Just say **"Tjenare"**, **"Hej Anders"** or **"Hey Anders"** and ask something:

> "Hej Anders, vad blir det för väder imorgon?"
> "Hey Anders, set a timer for 10 minutes."
> "Tjenare Anders, lägg till mjölk på inköpslistan."

> ⚠️ **Please use Anders exactly as it is.** Anders is built by Andreas, with his voice and personality.
> Don't change the code, voice or settings files. If something should be different, tell Andreas.

---

## ✅ Before you start: do you have the right computer?

| You need | Why |
|---|---|
| **Ubuntu Linux** (24.04 or newer) | Anders is built for Linux |
| **An NVIDIA graphics card with at least 8 GB memory** | Runs the brain and the voice. Check with `nvidia-smi` in a terminal |
| **16 GB RAM or more** (32 GB is better) | The AI brain is big |
| **About 40 GB free disk space** | For the AI models |
| **A microphone and speakers** (a headset works best) | So you can talk to it |

> 💡 No NVIDIA card? Anders still starts, but answers slowly and with a more robotic voice.

---

## 📦 What's included

Everything Anders needs comes with this download, in the exact versions Andreas uses:
**Anders** itself, **OpenJarvis** (the engine), **Chatterbox** (the voice), **Ollama** (runs the brain),
Andreas' voice, and the backup Swedish voice.

The installer then fetches the very large parts (the AI brain, about 13 GB, and the voice/graphics libraries)
automatically, because they are too big for GitHub. You don't have to do anything extra.

---

## 🛠️ Installation (about 1 hour, mostly waiting for downloads)

Open a **terminal** (press `Ctrl + Alt + T`) and copy-paste one step at a time.
When it asks for your password, type it and press Enter. You won't see the letters as you type, which is normal.

### Step 1: Install the basics

```bash
sudo apt update
sudo apt install -y git curl zstd libportaudio2 python3-gi gir1.2-webkit2-4.1 ubuntu-drivers-common
sudo ubuntu-drivers install
```

The last line installs the NVIDIA graphics driver. **Restart the computer** after this step.

### Step 2: Download Anders

```bash
git clone https://github.com/andreashjonsson/Anders-AI.git
```

### Step 3: Install Anders

```bash
cd Anders-AI
./install.sh
```

This installs everything and downloads about 20 GB, so grab a coffee ☕.
If it gets interrupted, just run `./install.sh` again. It continues where it stopped.

### Step 4: Start Anders 🎉

Open **Anders** from the app menu (press the Super/Windows key and type "Anders"), or type:

```bash
anders-app
```

The **first time** takes a few minutes while the voice and hearing load. Then say **"Hej Anders!"**

---

## 🎙️ Using Anders

- **Talk:** Say "Tjenare" or "Hej Anders" (Swedish) or "Hey Anders" (English), then your question. Anders replies in the same language. "Tjenare" always means Swedish.
- **Follow-up:** Right after Anders answers, you have 5 seconds to say more without "Hej Anders" again.
- **Type:** You can also type in the chat box in the window.
- **Weather where you live:** Click the ⚙️ gear icon and enter your home town.
- **Terminal mode:** Type `anders` in a terminal to run without a window.
- **Close:** Closing the window turns Anders off completely.

Things Anders can do: weather, timers and alarms, shopping/to-do lists, electricity prices, open apps,
remember things you tell it, and answer general questions.

**Tips on real places:** ask things like "planera en date i Karlstad" or "bra restauranger i Karlstad". Anders searches
the web, only names places that are in the search results (a fact check removes anything made up), and shows the
sources as links under the answer. Longer answers are shown as neat lists; only the first sentence is read aloud.

**Under every answer:** 📋 **Kopiera** copies it, 🔄 **Nytt svar** asks again for a different answer.

**Jokes:** "berätta ett skämt", "ett svart skämt", "ett skämt om datorer", then "ett till". About 95 hand-written jokes
(also in English: "tell me a joke").

**Roasts:** insult Anders ("du är dum", "håll käften") or say "roasta mig" / "roasta Kalle" and he hits back with one of
140+ hand-written comebacks (Swedish and English). Questions and commands are never roasted. Turn it off under ⚙️ → **Roasts**.

**Singing:** ask "kan du sjunga?" and Anders recites *Ack Värmland*.

**Saying bye:** "hejdå", "god natt" or "bye" gets a goodbye, and now and then a fun one ("Kryssa lugnt!", "See you later, alligator!").

**Fair warning:** Anders is a Värmlänning with a loose tongue. He swears now and then ("se på fan", "jävlar"), but never when you're sad.

**No internet?** Anders says so instead of guessing.

---

## 🆘 Something's not working?

| Problem | Try this |
|---|---|
| Anders doesn't hear me | Check that the right microphone is selected under **Settings → Sound** in Ubuntu |
| Anders hears me but doesn't answer | Run `ollama list`. It should show `gpt-oss-coder`. If not, run `./install.sh` again |
| The voice sounds robotic | The real voice needs the NVIDIA card. Check that `nvidia-smi` works, then restart the computer |
| The installer says "Do step 1 first" | Run the step 1 commands again |
| Anders answers things nobody said (TV, background noise) | He ignores unclear speech, and follow-ups without "Hej Anders" must be about as loud as your voice. Move the TV volume down or sit closer to the mic |
| Something else | Send Andreas the log: `~/.openjarvis/.state/anders.log` |

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
| Brain (LLM) | `gpt-oss:20b` via [Ollama](https://ollama.com) 0.40.15 (`config/gpt-oss-coder.Modelfile`) |
| Hearing (speech-to-text) | KBLab `kb-whisper-small`, Whisper trained on Swedish |
| Voice (text-to-speech) | [Chatterbox](https://github.com/resemble-ai/chatterbox) 0.1.7 cloning Andreas' voice (GPU), [Piper](https://github.com/rhasspy/piper) as a backup |
| Framework | [OpenJarvis](https://github.com/open-jarvis/OpenJarvis) 1.0.0 |
| App | FastAPI backend + web UI in a GTK/WebKit window |

| Folder | Contents |
|---|---|
| `app/` | Backend, tools, memory, alarms, system control, UI |
| `app/roasts.txt`, `app/jokes.txt`, `app/goodbyes.txt` | The roast, joke and goodbye libraries (one per line) |
| `tests/` | Quick checks: `~/.openjarvis/.venv/bin/python tests/test_anders.py` |
| `plugins/` | Wake-word listening and the bilingual voice client |
| `chatterbox/` | The local voice server |
| `bin/` | The `anders` and `anders-app` commands |
| `config/` | Personality, settings, model and Ollama service config |
| `voices/` | Andreas' voice and the Piper backup voice |
| `vendor/` | Bundled OpenJarvis source and Chatterbox package |
| `requirements/` | Exact versions of every Python package |
| `desktop/` | App menu entries and icon |

Installed to: `~/.openjarvis` (Anders, OpenJarvis, Chatterbox), `~/.local/ollama` (Ollama, runs as a user service),
`~/.local/bin` (commands).

</details>
