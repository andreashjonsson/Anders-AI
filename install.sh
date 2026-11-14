#!/usr/bin/env bash
# Installs Anders and everything it runs on: Ollama, OpenJarvis and Chatterbox.
# Uses the exact versions Anders was built and tested with. Safe to run again.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OJ="$HOME/.openjarvis"
OLLAMA_VERSION="0.40.15"
OLLAMA_DIR="$HOME/.local/ollama"
export PATH="$OLLAMA_DIR/bin:$HOME/.local/bin:$HOME/.local/uv:$HOME/.cargo/bin:$PATH"

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mStopped: %s\033[0m\n' "$*"; exit 1; }

for tool in git curl zstd; do
    command -v "$tool" >/dev/null || fail "'$tool' is missing. Do step 1 in the README first."
done
[[ -e /usr/lib/x86_64-linux-gnu/libportaudio.so.2 ]] || fail "The sound library is missing. Do step 1 in the README first."

# ---------------------------------------------------------------- Ollama
step "1/7 Ollama $OLLAMA_VERSION (runs the AI brain)"
if [[ "$("$OLLAMA_DIR/bin/ollama" --version 2>/dev/null | grep -o '[0-9.]*$')" != "$OLLAMA_VERSION" ]]; then
    systemctl --user stop ollama 2>/dev/null || true
    rm -rf "$OLLAMA_DIR"; mkdir -p "$OLLAMA_DIR"
    curl -fL --progress-bar \
        "https://github.com/ollama/ollama/releases/download/v$OLLAMA_VERSION/ollama-linux-amd64.tar.zst" \
        | zstd -d | tar -x -C "$OLLAMA_DIR"
fi
mkdir -p "$HOME/.config/systemd/user"
cp "$HERE/config/ollama.service" "$HOME/.config/systemd/user/ollama.service"
systemctl --user daemon-reload
systemctl --user enable --now ollama >/dev/null 2>&1 || systemctl --user restart ollama
for _ in $(seq 60); do ollama list >/dev/null 2>&1 && break; sleep 1; done
ollama list >/dev/null 2>&1 || fail "Ollama didn't start. See: journalctl --user -u ollama"
grep -q '.local/ollama/bin' "$HOME/.bashrc" 2>/dev/null \
    || echo 'export PATH="$HOME/.local/ollama/bin:$HOME/.local/bin:$HOME/.local/uv:$PATH"' >> "$HOME/.bashrc"

# ------------------------------------------------------------ OpenJarvis
step "2/7 OpenJarvis (Anders' engine, bundled in vendor/openjarvis)"
mkdir -p "$OJ"
[[ -f "$OJ/src/pyproject.toml" ]] || { rm -rf "$OJ/src"; cp -r "$HERE/vendor/openjarvis" "$OJ/src"; }
if [[ ! -d "$OJ/src/.git" ]]; then
    # OpenJarvis' installer downloads its own copy unless it finds a git repo here.
    git -C "$OJ/src" init -q
    git -C "$OJ/src" add -A
    git -C "$OJ/src" -c user.name=anders -c user.email=anders@localhost commit -q -m "OpenJarvis bundled with Anders"
fi
# The bundled source has no version tag, so tell the build which version it is.
export SETUPTOOLS_SCM_PRETEND_VERSION=1.0.0
if "$OJ/.venv/bin/python" -c "import openjarvis" 2>/dev/null; then
    echo "    already installed"
else
    export UV_VENV_CLEAR=1  # replace a half-finished venv from an interrupted run
    bash "$OJ/src/scripts/install/install.sh" --minimal --no-bg-orchestrator --no-analytics
fi

step "3/7 OpenJarvis' fast memory module (builds from source, takes 5-15 min the first time)"
if ! "$OJ/.venv/bin/python" -c "import openjarvis_rust" 2>/dev/null; then
    bash "$OJ/.scripts/install-rust.sh"
    bash "$OJ/.scripts/build-extension.sh" || echo "    (Build failed - Anders still works. Log: $OJ/.state/extension-build.log)"
fi

step "4/7 Speech recognition and backup voice"
uv pip install -q --no-deps --python "$OJ/.venv/bin/python" -r "$HERE/requirements/openjarvis-extras.txt"

# ------------------------------------------------------------ Chatterbox
step "5/7 Chatterbox (Anders' voice engine) - downloads a few GB"
[[ -x "$OJ/chatterbox/.venv/bin/python" ]] || uv venv -q --python 3.12 "$OJ/chatterbox/.venv"
uv pip install -q --no-deps --python "$OJ/chatterbox/.venv/bin/python" \
    -r "$HERE/requirements/chatterbox.txt" "$HERE"/vendor/chatterbox/*.whl

# ---------------------------------------------------------------- Anders
step "6/7 Anders"
mkdir -p "$OJ/app/static" "$OJ/plugins" "$OJ/voices/piper" "$HOME/.local/bin" \
         "$HOME/.local/share/applications" "$HOME/.local/share/icons"
for f in "$HERE"/app/*.py; do
    # Keep your own song list if you have one.
    [[ "$(basename "$f")" == anders_songs.py && -f "$OJ/app/anders_songs.py" ]] || cp "$f" "$OJ/app/"
done
cp "$HERE"/app/*.txt "$OJ/app/"  # roast and joke libraries
cp "$HERE"/app/static/* "$OJ/app/static/"
cp "$HERE"/plugins/*.py "$OJ/plugins/"
cp "$HERE"/chatterbox/server.py "$OJ/chatterbox/"
install -m 755 "$HERE"/bin/* "$HOME/.local/bin/"
cp "$HERE/config/SOUL.md" "$OJ/SOUL.md"
cp "$HERE/config/config.example.toml" "$OJ/config.toml"
[[ -f "$OJ/anders-settings.json" ]] || cp "$HERE/config/anders-settings.example.json" "$OJ/anders-settings.json"
cp "$HERE/voices/reference.wav" "$OJ/voices/reference.wav"
cp "$HERE"/voices/piper/* "$OJ/voices/piper/"
cp "$HERE/desktop/anders.png" "$HOME/.local/share/icons/anders.png"
for f in "$HERE"/desktop/*.desktop.in; do
    sed "s|@HOME@|$HOME|g" "$f" > "$HOME/.local/share/applications/$(basename "${f%.in}")"
done

step "7/7 Anders' brain (gpt-oss, about 13 GB - this takes a while)"
if ! ollama list | grep -q '^gpt-oss-coder'; then
    ollama pull gpt-oss:20b
    ollama create gpt-oss-coder -f "$HERE/config/gpt-oss-coder.Modelfile"
fi

printf '\n\033[32m✔ Anders is installed!\033[0m\n'
echo "Open 'Anders' from the app menu, or type: anders-app"
echo "The first start downloads the voice and hearing models (a few minutes). Then say \"Hej Anders\"."
