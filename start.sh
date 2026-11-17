#!/usr/bin/env bash
# One-command start: installs deps (first run), builds the UI, starts the server on http://localhost:8000
set -e
cd "$(dirname "$0")"
command -v tesseract >/dev/null || { echo "Tesseract OCR is required (apt install tesseract-ocr poppler-utils / brew install tesseract poppler)"; exit 1; }
python3 -m pip install -q -r backend/requirements.txt
if [ ! -d frontend/dist ]; then (cd frontend && npm install && npm run build); fi
cd backend && exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
