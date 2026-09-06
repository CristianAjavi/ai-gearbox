# cambiar

Pasa el trabajo en curso de un CLI de IA a otro sin perder el contexto ni los procesos.
Entiende tres CLIs, cada uno con su propia licencia: `claude` (Claude Code), `codex` (Codex CLI)
y `agy` (Antigravity CLI de Google).

No es una terminal única con los tres proveedores dentro. Anthropic veta el uso de la suscripción
en interfaces de terceros y Google prohíbe usar Antigravity fuera de sus productos. Por eso cada CLI
oficial sigue siendo el que habla con su proveedor, y `cambiar` solo mueve el trabajo entre ellos.

## Uso

```bash
cambiar codex                  # de la sesión más reciente de esta carpeta a Codex
cambiar agy --desde claude     # fija el CLI de origen
cambiar claude --sesion 7cdc   # sesión concreta, por prefijo del id
cambiar codex --seco           # imprime el traspaso y no lanza nada
cambiar --listar               # sesiones de esta carpeta, la más reciente arriba
cambiar --listar --todas       # de todas las carpetas
cambiar --leer agy 7cdc140e    # vuelca una sesión completa en texto legible
cambiar --fondo make render    # proceso que sobrevive al cambio de CLI
cambiar --procesos             # estado de los procesos de fondo y su log
```

## Qué viaja

Un traspaso de como mucho 9.000 caracteres, unos 2.200 tokens, con:

- el encargo, primer mensaje real del usuario;
- el estado actual, última respuesta del asistente de origen;
- los últimos seis mensajes del usuario;
- los archivos tocados y los comandos recientes;
- la ruta de la sesión completa, para que el destino lea el detalle con `cambiar --leer` si lo necesita.

Un traspaso anterior que venga dentro de la conversación se sustituye por `[traspaso previo omitido]`,
así los cambios sucesivos no se anidan. Cada traspaso queda guardado en `~/.cambiar/traspasos/`.

## Qué no viaja

La conversación literal, los permisos, los hooks y los agentes del CLI de origen. El cambio ocurre
entre respuestas, nunca a mitad de una.

## De dónde lee

| CLI | Sesiones |
|---|---|
| claude | `~/.claude/projects/<carpeta>/<id>.jsonl` |
| codex | `~/.codex/sessions/AAAA/MM/DD/rollout-*.jsonl` |
| agy | `~/.gemini/antigravity-cli/conversations/<id>.db` (SQLite con pasos en protobuf) |

## Instalar y probar

```bash
./instalar.sh                  # enlaza ~/.local/bin/cambiar y corre las pruebas
python3 -m unittest -v test_cambiar
```

Solo necesita Python 3 del sistema. Las pruebas crean sesiones sintéticas de los tres formatos
en una carpeta temporal e incluyen controles negativos: carpeta sin sesiones, sesión ilegible,
protobuf roto y ejecutable de destino simulado.
