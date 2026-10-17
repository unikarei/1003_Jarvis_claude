#!/bin/bash
# JARVIS と声で会話する。Finder でダブルクリック、またはターミナルで ./run_voice_chat.command
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo ".venv がありません。README の「セットアップ（Mac mini）」を参照してください。"
  read -r -p "Enter で閉じる" _
  exit 1
fi
.venv/bin/python voice_chat.py
