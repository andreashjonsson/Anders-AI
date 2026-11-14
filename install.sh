#!/usr/bin/env bash
# Installs Anders on top of an existing OpenJarvis install (~/.openjarvis).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OJ="$HOME/.openjarvis"
[[ -d "$OJ" ]] || { echo "OpenJarvis not found in $OJ - install it first (see README)."; exit 1; }

mkdir -p "$OJ/app/static" "$OJ/plugins" "$OJ/chatterbox" "$OJ/voices" "$HOME/.local/bin" \
         "$HOME/.local/share/applications" "$HOME/.local/share/icons"
cp "$HERE"/app/*.py "$OJ/app/"
cp "$HERE"/app/static/* "$OJ/app/static/"
cp "$HERE"/plugins/*.py "$OJ/plugins/"
cp "$HERE"/chatterbox/server.py "$OJ/chatterbox/"
install -m 755 "$HERE"/bin/* "$HOME/.local/bin/"

# Config: only copied if you don't already have one.
[[ -f "$OJ/SOUL.md" ]]               || cp "$HERE/config/SOUL.md" "$OJ/"
[[ -f "$OJ/config.toml" ]]           || cp "$HERE/config/config.example.toml" "$OJ/config.toml"
[[ -f "$OJ/anders-settings.json" ]]  || cp "$HERE/config/anders-settings.example.json" "$OJ/anders-settings.json"

# Andreas' voice (Chatterbox clone) and icon. Your own recording wins if you already made one.
[[ -f "$OJ/voices/reference.wav" ]] || cp "$HERE/voices/reference.wav" "$OJ/voices/"
cp "$HERE/desktop/anders.png" "$HOME/.local/share/icons/anders.png"

for f in "$HERE"/desktop/*.desktop.in; do
    sed "s|@HOME@|$HOME|g" "$f" > "$HOME/.local/share/applications/$(basename "${f%.in}")"
done
echo "Done. Start with 'anders-app' (window) or 'anders' (terminal)."
echo "Optional: 'anders-record-voice' to clone your voice, 'anders-set-logo' to set the icon."
