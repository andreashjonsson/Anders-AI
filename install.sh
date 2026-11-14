#!/usr/bin/env bash
# Installs Anders on top of OpenJarvis (~/.openjarvis). Safe to run again.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OJ="$HOME/.openjarvis"
export PATH="$HOME/.local/bin:$HOME/.local/uv:$HOME/.cargo/bin:$PATH"

step() { printf '\n==> %s\n' "$*"; }

[[ -x "$OJ/.venv/bin/python" ]] || { echo "OpenJarvis is not installed yet (step 2 in the README)."; exit 1; }
command -v uv >/dev/null     || { echo "'uv' not found. It comes with OpenJarvis - open a new terminal and try again."; exit 1; }
command -v ollama >/dev/null || { echo "Ollama is not installed (step 2 in the README)."; exit 1; }

step "Copying Anders files"
mkdir -p "$OJ/app/static" "$OJ/plugins" "$OJ/chatterbox" "$OJ/voices/piper" "$OJ/tools" \
         "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/.local/share/icons"
cp "$HERE"/app/*.py "$OJ/app/"
cp "$HERE"/app/static/* "$OJ/app/static/"
cp "$HERE"/plugins/*.py "$OJ/plugins/"
cp "$HERE"/chatterbox/server.py "$OJ/chatterbox/"
install -m 755 "$HERE"/bin/* "$HOME/.local/bin/"
[[ -f "$OJ/SOUL.md" ]]              || cp "$HERE/config/SOUL.md" "$OJ/"
[[ -f "$OJ/anders-settings.json" ]] || cp "$HERE/config/anders-settings.example.json" "$OJ/anders-settings.json"
# Anders needs these settings in OpenJarvis' config (the original is backed up once).
if ! grep -q 'gpt-oss-coder' "$OJ/config.toml" 2>/dev/null; then
    [[ -f "$OJ/config.toml" ]] && cp -n "$OJ/config.toml" "$OJ/config.toml.before-anders"
    cp "$HERE/config/config.example.toml" "$OJ/config.toml"
fi
# Anders' voice and icon. A voice you recorded yourself is kept.
[[ -f "$OJ/voices/reference.wav" ]] || cp "$HERE/voices/reference.wav" "$OJ/voices/"
cp "$HERE/desktop/anders.png" "$HOME/.local/share/icons/anders.png"
for f in "$HERE"/desktop/*.desktop.in; do
    sed "s|@HOME@|$HOME|g" "$f" > "$HOME/.local/share/applications/$(basename "${f%.in}")"
done

step "Installing speech recognition and speech packages"
uv pip install -q --python "$OJ/.venv/bin/python" \
    faster-whisper piper-tts sounddevice soundfile fastapi uvicorn httpx scipy numpy

step "Installing the voice engine (Chatterbox) - this one is big, a few GB"
if [[ ! -x "$OJ/chatterbox/.venv/bin/python" ]]; then
    uv venv -q --python 3.12 "$OJ/chatterbox/.venv"
fi
uv pip install -q --python "$OJ/chatterbox/.venv/bin/python" \
    chatterbox-tts fastapi uvicorn soundfile pyloudnorm scipy

step "Installing the photo-to-icon tool"
[[ -x "$OJ/tools/.venv/bin/python" ]] || uv venv -q --python 3.13 "$OJ/tools/.venv"
uv pip install -q --python "$OJ/tools/.venv/bin/python" pillow numpy opencv-python-headless

step "Downloading the backup Swedish voice (Piper)"
BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/sv/sv_SE/nst/medium"
for f in sv_SE-nst-medium.onnx sv_SE-nst-medium.onnx.json; do
    [[ -s "$OJ/voices/piper/$f" ]] || curl -fL --progress-bar -o "$OJ/voices/piper/$f" "$BASE/$f"
done

step "Downloading Anders' brain (gpt-oss, about 13 GB - this takes a while)"
if ! ollama list | grep -q '^gpt-oss-coder'; then
    ollama pull gpt-oss:20b
    ollama create gpt-oss-coder -f "$HERE/config/gpt-oss-coder.Modelfile"
fi

step "Done!"
echo "Open 'Anders' from the app menu, or type: anders-app"
echo "The first start downloads the voice model (a few minutes). Then say \"Hej Anders\"."
