#!/bin/sh
# Enlaza `cambiar` en ~/.local/bin y corre las pruebas. Idempotente.
set -e
AQUI=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$HOME/.local/bin"
chmod +x "$AQUI/cambiar.py"
ln -sfn "$AQUI/cambiar.py" "$HOME/.local/bin/cambiar"
echo "enlazado: $HOME/.local/bin/cambiar -> $AQUI/cambiar.py"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "aviso: ~/.local/bin no está en el PATH" ;;
esac
cd "$AQUI" && python3 -m unittest test_cambiar
