#!/bin/sh
# Links `gearbox` into ~/.local/bin and runs the tests. Idempotent.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$HOME/.local/bin"
chmod +x "$HERE/gearbox.py"
ln -sfn "$HERE/gearbox.py" "$HOME/.local/bin/gearbox"
echo "linked: $HOME/.local/bin/gearbox -> $HERE/gearbox.py"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "warning: ~/.local/bin is not on PATH" ;;
esac
cd "$HERE" && python3 -m unittest test_gearbox
