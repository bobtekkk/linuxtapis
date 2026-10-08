#!/bin/sh
# Starts Tapis. The first run sets up a private Python environment in .venv.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
if [ ! -x "$DIR/.venv/bin/python" ]; then
    python3 -m venv --system-site-packages "$DIR/.venv"
    "$DIR/.venv/bin/pip" install -q numpy PyOpenGL
fi
cd "$DIR"
exec "$DIR/.venv/bin/python" -m tapis "$@"
