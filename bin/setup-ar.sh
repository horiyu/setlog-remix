#!/bin/bash
# Set up what the AR effects (particles, pin, speech, aura, background, glitch, neon,
# trail) need: a Python in .venv with numpy, OpenCV, onnxruntime and Pillow, and the two
# small models (person matte ~25 MB, depth ~27 MB) in cache/models. CPU only; no GPU needed.
set -e
cd "$(dirname "$0")/.."
if command -v uv >/dev/null; then
  [ -x .venv/bin/python ] || uv venv -q .venv
  uv pip install -q --python .venv/bin/python -r requirements-ar.txt
else
  [ -x .venv/bin/python ] || python3 -m venv .venv
  .venv/bin/python -m pip install -q -r requirements-ar.txt
fi
.venv/bin/python ar.py --fetch-models
echo "AR effects ready (.venv, cache/models)."
