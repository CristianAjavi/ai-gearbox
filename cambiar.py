#!/usr/bin/env python3
"""cambiar: pasa el trabajo en curso de un CLI de IA a otro sin perder el contexto ni los procesos.

CLIs que entiende: claude (Claude Code), codex (Codex CLI), agy (Antigravity CLI).
Cada uno conserva su propio login. Lo que viaja es un traspaso compacto: el encargo,
el estado actual, los últimos mensajes, los archivos tocados y los comandos recientes,
más la ruta de la sesión completa por si el destino necesita el detalle.

Uso:
  cambiar codex                   de la sesión más reciente de esta carpeta a Codex
  cambiar agy --desde claude      fija el CLI de origen
  cambiar claude --sesion 7cdc    sesión concreta, por prefijo del id
  cambiar codex --seco            imprime el traspaso y no lanza nada
  cambiar --listar                sesiones de esta carpeta, la más reciente arriba
  cambiar --leer agy 7cdc140e     vuelca una sesión completa en texto legible
  cambiar --fondo <cmd...>        lanza un proceso que sobrevive al cambio de CLI
  cambiar --procesos              lista los procesos de fondo y su estado

Códigos de salida: 0 bien · 2 no hay sesión · 3 la sesión no tiene mensajes legibles · 4 uso.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field

TOOLS = ("claude", "codex", "agy")
MAX_CHARS = 9000            # ≈ 2.200 tokens; el techo del traspaso
MARCA = "# Traspaso de sesión"
MARCAS_AJENAS = ("I'm continuing a coding session", "## Session Handoff Context")
DIR_CAMBIAR = os.path.join(os.path.expanduser("~"), ".cambiar")


@dataclass
class Msg:
    role: str                       # user | assistant | tool
    text: str = ""
    tool: str = ""
    args: dict = field(default_factory=dict)


@dataclass
class Session:
    tool: str
    id: str
    path: str
    cwd: str
    mtime: float
    msgs: list = field(default_factory=list)

    @property
    def short(self) -> str:
        return self.id[:8]


# ----------------------------------------------------------------------------- utilidades

def _home() -> str:
    return os.path.expanduser("~")


def _es_traspaso(text: str) -> bool:
    head = text.lstrip()[:400]
    return head.startswith(MARCA) or any(m in head for m in MARCAS_AJENAS)


def _limpia(text: str) -> str:
    return re.sub(r"[ \t]+\n", "\n", text).strip()


def _recorta(text: str, n: int) -> str:
    text = _limpia(text)
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _json_o_vacio(s):
    if isinstance(s, dict):
        return s
    try:
        d = json.loads(s)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


# ----------------------------------------------------------------------------- protobuf mínimo (agy)

def _varint(b: bytes, i: int):
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if not c & 0x80:
            return r, i


def pb_campos(b: bytes) -> dict:
    """Devuelve {numero_de_campo: [valores]} del primer nivel de un mensaje protobuf.
    Los valores de longitud variable quedan como bytes; el llamador decide si son texto o mensaje."""
    out: dict = {}
    i = 0
    n = len(b)
    while i < n:
        tag, i = _varint(b, i)
        f, wt = tag >> 3, tag & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 1:
            v, i = b[i:i + 8], i + 8
        elif wt == 5:
            v, i = b[i:i + 4], i + 4
        elif wt == 2:
            ln, i = _varint(b, i)
            v, i = b[i:i + ln], i + ln
        else:                       # grupos: no se esperan; se corta sin inventar
            break
        out.setdefault(f, []).append(v)
    return out


def pb_ruta(b: bytes, ruta: str) -> list:
    """Valores (bytes) en la ruta '20.7.2': recorre mensajes anidados por número de campo."""
    partes = [int(p) for p in ruta.split(".")]
    actuales = [b]
    for p in partes:
        siguientes = []
        for m in actuales:
            try:
                siguientes.extend(v for v in pb_campos(m).get(p, []) if isinstance(v, bytes))
            except Exception:
                continue
        actuales = siguientes
        if not actuales:
            return []
    return actuales


def pb_texto(b: bytes, ruta: str) -> str:
    for v in pb_ruta(b, ruta):
        try:
            t = v.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if t and all(ord(c) >= 32 or c in "\n\t\r" for c in t):
            return t
    return ""


# ----------------------------------------------------------------------------- parsers

def _claude_dir_proyecto(cwd: str) -> str:
    return os.path.join(_home(), ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", cwd))


def sesiones_claude(cwd: str | None) -> list:
    dirs = [_claude_dir_proyecto(cwd)] if cwd else glob.glob(os.path.join(_home(), ".claude", "projects", "*"))
    out = []
    for d in dirs:
        for p in glob.glob(os.path.join(d, "*.jsonl")):
            sid = os.path.basename(p)[:-6]
            out.append(Session("claude", sid, p, cwd or "", os.path.getmtime(p)))
    return out


def parse_claude(s: Session) -> Session:
    msgs = []
    with open(s.path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("isSidechain") or d.get("isMeta"):
                continue
            if not s.cwd and d.get("cwd"):
                s.cwd = d["cwd"]
            t = d.get("type")
            content = (d.get("message") or {}).get("content")
            if t == "user":
                if isinstance(content, str):
                    if not content.lstrip().startswith("<"):
                        msgs.append(Msg("user", content))
                elif isinstance(content, list):
                    for blk in content:
                        if blk.get("type") == "text" and not blk.get("text", "").lstrip().startswith("<"):
                            msgs.append(Msg("user", blk["text"]))
            elif t == "assistant" and isinstance(content, list):
                for blk in content:
                    if blk.get("type") == "text" and blk.get("text", "").strip():
                        msgs.append(Msg("assistant", blk["text"]))
                    elif blk.get("type") == "tool_use":
                        msgs.append(Msg("tool", tool=blk.get("name", ""), args=blk.get("input") or {}))
    s.msgs = msgs
    return s


def sesiones_codex(cwd: str | None) -> list:
    out = []
    for p in glob.glob(os.path.join(_home(), ".codex", "sessions", "**", "rollout-*.jsonl"), recursive=True):
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                meta = json.loads(fh.readline())
        except Exception:
            continue
        pl = meta.get("payload") or {}
        scwd = pl.get("cwd", "")
        if cwd and scwd != cwd:
            continue
        sid = pl.get("id") or pl.get("session_id") or os.path.basename(p)
        out.append(Session("codex", sid, p, scwd, os.path.getmtime(p)))
    return out


def parse_codex(s: Session) -> Session:
    msgs = []
    with open(s.path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") != "response_item":
                continue
            pl = d.get("payload") or {}
            kind = pl.get("type")
            if kind == "message":
                role = pl.get("role")
                if role not in ("user", "assistant"):
                    continue
                for c in pl.get("content") or []:
                    txt = c.get("text", "")
                    if not txt.strip() or txt.lstrip().startswith("<"):
                        continue
                    msgs.append(Msg(role, txt))
            elif kind == "function_call":
                msgs.append(Msg("tool", tool=pl.get("name", ""), args=_json_o_vacio(pl.get("arguments", "")) or {"raw": pl.get("arguments", "")}))
            elif kind == "custom_tool_call":
                msgs.append(Msg("tool", tool=pl.get("name", ""), args={"raw": pl.get("input", "")}))
    s.msgs = msgs
    return s


def _agy_dir() -> str:
    return os.path.join(_home(), ".gemini", "antigravity-cli")


def _agy_workspaces() -> dict:
    """conversationId -> workspace, leído del history.jsonl del CLI."""
    m = {}
    p = os.path.join(_agy_dir(), "history.jsonl")
    if not os.path.exists(p):
        return m
    with open(p, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("conversationId"):
                m[d["conversationId"]] = d.get("workspace", "")
    return m


def sesiones_agy(cwd: str | None) -> list:
    ws = _agy_workspaces()
    out = []
    for p in glob.glob(os.path.join(_agy_dir(), "conversations", "*.db")):
        sid = os.path.basename(p)[:-3]
        scwd = ws.get(sid, "")
        if cwd and scwd != cwd:
            continue
        out.append(Session("agy", sid, p, scwd, os.path.getmtime(p)))
    return out


AGY_USER, AGY_ASSISTANT = 14, 15


def parse_agy(s: Session) -> Session:
    msgs = []
    con = sqlite3.connect(f"file:{s.path}?mode=ro", uri=True)
    try:
        rows = con.execute("select idx, step_type, step_payload from steps order by idx").fetchall()
    finally:
        con.close()
    for _idx, st, payload in rows:
        if not payload:
            continue
        if st == AGY_USER:
            t = pb_texto(payload, "19.2")
            if t.strip():
                msgs.append(Msg("user", t))
        elif st == AGY_ASSISTANT:
            t = pb_texto(payload, "20.1")
            if t.strip():
                msgs.append(Msg("assistant", t))
            name = pb_texto(payload, "20.7.2")
            if name:
                msgs.append(Msg("tool", tool=name, args=_json_o_vacio(pb_texto(payload, "20.7.3"))))
    s.msgs = msgs
    return s


LISTADORES = {"claude": sesiones_claude, "codex": sesiones_codex, "agy": sesiones_agy}
PARSERS = {"claude": parse_claude, "codex": parse_codex, "agy": parse_agy}


def listar(cwd: str | None, desde: str | None = None) -> list:
    out = []
    for t in TOOLS:
        if desde and t != desde:
            continue
        out.extend(LISTADORES[t](cwd))
    return sorted(out, key=lambda s: s.mtime, reverse=True)


# ----------------------------------------------------------------------------- extracción del estado

RE_PATCH = re.compile(r"\*\*\* (?:Update|Add|Delete) File: (.+)")


def archivos_tocados(msgs: list) -> list:
    vistos, out = set(), []

    def add(p):
        if p and p not in vistos:
            vistos.add(p)
            out.append(p)

    for m in msgs:
        if m.role != "tool":
            continue
        a = m.args or {}
        if m.tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            add(a.get("file_path") or a.get("notebook_path"))
        elif m.tool in ("write_to_file", "replace_file_content", "multi_replace_file_content", "edit_file", "create_file"):
            add(a.get("TargetFile") or a.get("AbsolutePath") or a.get("target_file") or a.get("path"))
        elif m.tool == "apply_patch":
            for p in RE_PATCH.findall(a.get("input") or a.get("raw") or json.dumps(a)):
                add(p.strip())
    return out


def comandos_recientes(msgs: list, n: int = 5) -> list:
    out = []
    for m in msgs:
        if m.role != "tool":
            continue
        a = m.args or {}
        c = ""
        if m.tool == "Bash":
            c = a.get("command", "")
        elif m.tool in ("shell", "exec_command", "shell_command", "run_command", "exec"):
            c = a.get("command") or a.get("cmd") or a.get("CommandLine") or a.get("raw") or ""
            if isinstance(c, list):
                c = " ".join(c)
        if c:
            out.append(c.strip().splitlines()[0])
    return out[-n:]


def construir_traspaso(s: Session, destino: str, max_chars: int = MAX_CHARS) -> str:
    usuarios = [m for m in s.msgs if m.role == "user"]
    asistente = [m for m in s.msgs if m.role == "assistant"]
    encargo = next((m.text for m in usuarios if not _es_traspaso(m.text)), "")
    ultimos = [("[traspaso previo omitido]" if _es_traspaso(m.text) else m.text) for m in usuarios[-6:]]
    estado = asistente[-1].text if asistente else "(la sesión de origen no tiene respuesta del asistente: retoma desde los mensajes del usuario)"
    archivos = archivos_tocados(s.msgs)[-15:]
    comandos = comandos_recientes(s.msgs)
    fecha = dt.datetime.fromtimestamp(s.mtime).strftime("%Y-%m-%d %H:%M")

    presupuesto = {"estado": 1800, "encargo": 700, "ultimo": 240, "cmd": 120}
    while True:
        partes = [
            f"{MARCA}",
            f"Origen: {s.tool} · sesión {s.id} · {fecha}",
            f"Archivo de la sesión completa: {s.path}",
            f"Directorio de trabajo: {s.cwd or os.getcwd()} · Destino: {destino}",
            "",
            "## Encargo",
            _recorta(encargo, presupuesto["encargo"]) or "(sin mensaje inicial legible)",
            "",
            "## Estado actual (última respuesta del asistente de origen)",
            _recorta(estado, presupuesto["estado"]),
            "",
            "## Últimos mensajes del usuario",
        ]
        partes += [f"- {_recorta(u, presupuesto['ultimo'])}" for u in ultimos] or ["- (ninguno)"]
        if archivos:
            partes += ["", "## Archivos tocados"] + [f"- {p}" for p in archivos]
        if comandos:
            partes += ["", "## Comandos recientes"] + [f"- `{_recorta(c, presupuesto['cmd'])}`" for c in comandos]
        partes += [
            "",
            "## Cómo seguir",
            "Continúa desde el estado actual. No repitas lo ya hecho ni vuelvas a preguntar lo ya respondido.",
            f"Si falta un detalle, léelo de la sesión completa con `cambiar --leer {s.tool} {s.id}` antes de preguntar.",
            "Los procesos largos van con `cambiar --fondo <cmd>` para que sobrevivan al próximo cambio.",
        ]
        texto = "\n".join(partes)
        if len(texto) <= max_chars:
            return texto
        # recorte en orden: comandos, archivos, mensajes, estado, encargo
        if comandos:
            comandos = []
        elif archivos:
            archivos = []
        elif presupuesto["ultimo"] > 80:
            presupuesto["ultimo"] = 80
        elif presupuesto["estado"] > 400:
            presupuesto["estado"] = max(400, presupuesto["estado"] // 2)
        elif presupuesto["encargo"] > 200:
            presupuesto["encargo"] = 200
        else:
            return texto[:max_chars]


def volcar(s: Session) -> str:
    out = [f"# Sesión {s.tool} {s.id}", f"Archivo: {s.path}", f"Directorio: {s.cwd}", ""]
    for m in s.msgs:
        if m.role == "tool":
            a = json.dumps(m.args, ensure_ascii=False)
            out.append(f"**[herramienta {m.tool}]** {a[:300]}")
        else:
            out.append(f"**{m.role}:**\n{m.text.strip()}")
        out.append("")
    return "\n".join(out)


# ----------------------------------------------------------------------------- lanzamiento y procesos

def comando_destino(destino: str, prompt: str) -> list:
    if destino == "claude":
        return ["claude", prompt]
    if destino == "codex":
        return ["codex", prompt]
    if destino == "agy":
        return ["agy", "-i", prompt]
    raise ValueError(destino)


def guardar_traspaso(texto: str, origen: str, destino: str) -> str:
    d = os.path.join(DIR_CAMBIAR, "traspasos")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{dt.datetime.now():%Y-%m-%d-%H%M%S}-{origen}-a-{destino}.md")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(texto)
    return p


def lanzar_fondo(cmd: list, cwd: str) -> dict:
    d = os.path.join(DIR_CAMBIAR, "procesos")
    os.makedirs(d, exist_ok=True)
    ts = f"{dt.datetime.now():%Y-%m-%d-%H%M%S}"
    log = os.path.join(d, f"{ts}.log")
    with open(log, "ab") as fh:
        p = subprocess.Popen(cmd, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
    reg = {"pid": p.pid, "cmd": cmd, "cwd": cwd, "log": log, "inicio": ts}
    with open(os.path.join(d, f"{ts}.json"), "w", encoding="utf-8") as fh:
        json.dump(reg, fh, ensure_ascii=False)
    return reg


def _vivo(pid: int) -> bool:
    """Vivo de verdad: ni terminado ni zombi. Si es hijo nuestro se recoge aquí mismo."""
    try:
        wpid, _ = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        estado = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return True
    return bool(estado) and not estado.startswith("Z")


def listar_procesos() -> list:
    d = os.path.join(DIR_CAMBIAR, "procesos")
    out = []
    for p in sorted(glob.glob(os.path.join(d, "*.json"))):
        try:
            with open(p, encoding="utf-8") as fh:
                reg = json.load(fh)
        except Exception:
            continue
        reg["vivo"] = _vivo(int(reg["pid"]))
        out.append(reg)
    return out


# ----------------------------------------------------------------------------- CLI

def _buscar(cwd: str, desde: str | None, sesion: str | None) -> Session | None:
    cand = listar(cwd if not sesion else None, desde)
    if sesion:
        cand = [s for s in cand if s.id.startswith(sesion)]
    return cand[0] if cand else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cambiar", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("destino", nargs="?", choices=TOOLS, help="CLI al que te cambias")
    ap.add_argument("--desde", choices=TOOLS, help="CLI de origen (por defecto, la sesión más reciente de esta carpeta)")
    ap.add_argument("--sesion", help="prefijo del id de la sesión de origen")
    ap.add_argument("--seco", action="store_true", help="imprime el traspaso y no lanza nada")
    ap.add_argument("--max-chars", type=int, default=MAX_CHARS, help=f"techo del traspaso (por defecto {MAX_CHARS})")
    ap.add_argument("--listar", action="store_true", help="sesiones de esta carpeta")
    ap.add_argument("--todas", action="store_true", help="con --listar: de todas las carpetas")
    ap.add_argument("--leer", nargs=2, metavar=("CLI", "ID"), help="vuelca una sesión completa")
    ap.add_argument("--fondo", nargs=argparse.REMAINDER, help="lanza un proceso que sobrevive al cambio de CLI")
    ap.add_argument("--procesos", action="store_true", help="lista los procesos de fondo")
    a = ap.parse_args(argv)
    cwd = os.getcwd()

    if a.procesos:
        regs = listar_procesos()
        if not regs:
            print("Sin procesos de fondo registrados.")
        for r in regs:
            print(f"{'VIVO ' if r['vivo'] else 'FIN  '} pid {r['pid']:>6}  {r['inicio']}  {' '.join(r['cmd'])[:70]}  → {r['log']}")
        return 0

    if a.fondo is not None:
        if not a.fondo:
            print("cambiar --fondo <comando...>", file=sys.stderr)
            return 4
        r = lanzar_fondo(a.fondo, cwd)
        print(f"Lanzado pid {r['pid']} · log {r['log']}")
        return 0

    if a.listar:
        ses = listar(None if a.todas else cwd)
        if not ses:
            print("No hay sesiones" + ("" if a.todas else " en esta carpeta") + ".")
            return 2
        for s in ses[:30]:
            print(f"{s.tool:6} {s.short}  {dt.datetime.fromtimestamp(s.mtime):%Y-%m-%d %H:%M}  {s.cwd}")
        return 0

    if a.leer:
        tool, sid = a.leer
        if tool not in TOOLS:
            print(f"CLI desconocido: {tool}", file=sys.stderr)
            return 4
        s = _buscar(cwd, tool, sid)
        if not s:
            print(f"No hay sesión {tool} que empiece por {sid}.", file=sys.stderr)
            return 2
        PARSERS[tool](s)
        if not s.msgs:
            print(f"La sesión {s.id} existe pero no tiene mensajes legibles.", file=sys.stderr)
            return 3
        print(volcar(s))
        return 0

    if not a.destino:
        ap.print_help()
        return 4

    s = _buscar(cwd, a.desde, a.sesion)
    if not s:
        print("No hay sesión de origen" + (f" de {a.desde}" if a.desde else "") + " en esta carpeta. Prueba `cambiar --listar --todas`.", file=sys.stderr)
        return 2
    PARSERS[s.tool](s)
    if not s.msgs:
        print(f"La sesión {s.tool} {s.id} existe pero no tiene mensajes legibles. No se lanza nada.", file=sys.stderr)
        return 3
    if s.tool == a.destino and not a.seco:
        print(f"La sesión más reciente ya es de {s.tool}. Si quieres retomarla, usa su propio resume.", file=sys.stderr)
        return 4
    texto = construir_traspaso(s, a.destino, a.max_chars)
    if a.seco:
        print(texto)
        sys.stdout.flush()
        print(f"\n[{len(texto)} caracteres ≈ {len(texto)//4} tokens]", file=sys.stderr)
        return 0
    p = guardar_traspaso(texto, s.tool, a.destino)
    print(f"Traspaso {s.tool} → {a.destino}: {len(texto)} caracteres (≈{len(texto)//4} tokens), guardado en {p}", file=sys.stderr)
    cmd = comando_destino(a.destino, texto)
    try:
        os.execvp(cmd[0], cmd)
    except FileNotFoundError:
        print(f"No encuentro el ejecutable `{cmd[0]}` en el PATH.", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
