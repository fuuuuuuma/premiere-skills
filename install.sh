#!/usr/bin/env bash
set -e

REPO_URL="https://github.com/fuuuuuuma/premiere-skills.git"
TEMP_DIR="$(mktemp -d)"

echo "==> Downloading premiere-skills plugin from GitHub..."
git clone --depth 1 "$REPO_URL" "$TEMP_DIR"

echo "==> Installing plugin into Antigravity..."
if command -v agy >/dev/null 2>&1; then
  agy plugin install "$TEMP_DIR"
else
  mkdir -p "$HOME/.gemini/config/plugins"
  rm -rf "$HOME/.gemini/config/plugins/premiere-skills"
  cp -R "$TEMP_DIR" "$HOME/.gemini/config/plugins/premiere-skills"
  echo "  [ok] Copied plugin to ~/.gemini/config/plugins/premiere-skills"
fi

# Antigravity / Agents SDK の標準探索パス (~/.agents/skills/) にもシンボリックリンクを展開
mkdir -p "$HOME/.agents/skills"
for skill in cut srt-fast; do
  if [ -d "$HOME/.gemini/config/plugins/premiere-skills/skills/$skill" ]; then
    ln -sfn "$HOME/.gemini/config/plugins/premiere-skills/skills/$skill" "$HOME/.agents/skills/$skill"
  fi
done

rm -rf "$TEMP_DIR"
echo "==> Successfully installed premiere-skills plugin into Antigravity!"
echo "    Available skills: /cut, /srt-fast"
