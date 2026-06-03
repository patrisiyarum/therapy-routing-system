#!/usr/bin/env bash
set -e
python3 -m venv .venv 2>/dev/null || true
source .venv/bin/activate
pip install -q -r requirements.txt
[ -f .env ] && export $(grep -v '^#' .env | grep -v '^$' | xargs)
echo "Open http://127.0.0.1:8000"
uvicorn app.main:app --reload
