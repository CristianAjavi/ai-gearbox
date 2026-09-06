"""Pruebas de `cambiar`. Corren con HOME apuntando a una carpeta temporal con sesiones sintéticas
de los tres CLIs, y con controles negativos: carpeta sin sesiones y sesión sin mensajes legibles.

    python3 -m unittest -v test_cambiar
"""
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cambiar  # noqa: E402


# ----------------------------------------------------------------------------- codificador protobuf de prueba

def _vint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def pb(campos: dict) -> bytes:
    """{numero: valor} → bytes. valor: str, bytes (mensaje anidado), int, o lista de ellos."""
    out = bytearray()
    for f, vals in campos.items():
        for v in (vals if isinstance(vals, list) else [vals]):
            if isinstance(v, int):
                out += _vint((f << 3) | 0) + _vint(v)
            else:
                b = v.encode("utf-8") if isinstance(v, str) else v
                out += _vint((f << 3) | 2) + _vint(len(b)) + b
    return bytes(out)


# ----------------------------------------------------------------------------- fixtures

CWD = os.path.realpath("/tmp/proyecto-de-prueba")   # os.getcwd() devuelve la ruta real; en macOS /tmp es un enlace
TRASPASO_AJENO = "I'm continuing a coding session from **Claude Code** ...\n\n## Session Handoff Context\nmucho texto"


def escribe_claude(home, sid="aaaa1111-0000-0000-0000-000000000000", mensajes=None, mtime=None):
    d = os.path.join(home, ".claude", "projects", cambiar.re.sub(r"[^A-Za-z0-9]", "-", CWD))
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{sid}.jsonl")
    base = {"cwd": CWD, "sessionId": sid, "isSidechain": False}
    lineas = mensajes if mensajes is not None else [
        {**base, "type": "user", "message": {"role": "user", "content": "Arregla el validador de ingresos"}},
        {**base, "type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Voy a mirar el motor."},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls motores/"}},
        ]}},
        {**base, "type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "a.py"}]}},
        {**base, "type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "/tmp/proyecto-de-prueba/motores/a.py", "old_string": "x", "new_string": "y"}},
        ]}},
        {**base, "type": "user", "isSidechain": True, "message": {"role": "user", "content": "SOY UN SUBAGENTE: no debo aparecer"}},
        {**base, "type": "user", "message": {"role": "user", "content": TRASPASO_AJENO}},
        {**base, "type": "user", "message": {"role": "user", "content": "<local-command-stdout>ruido</local-command-stdout>"}},
        {**base, "type": "user", "message": {"role": "user", "content": "Ahora corre las pruebas"}},
        {**base, "type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "ESTADO FINAL: 3 pruebas verdes, falta el push."},
        ]}},
    ]
    with open(p, "w", encoding="utf-8") as fh:
        for l in lineas:
            fh.write(json.dumps(l, ensure_ascii=False) + "\n")
    if mtime:
        os.utime(p, (mtime, mtime))
    return p


def escribe_codex(home, sid="bbbb2222-0000-0000-0000-000000000000", cwd=CWD, mtime=None):
    d = os.path.join(home, ".codex", "sessions", "2026", "09", "05")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"rollout-2026-09-05T10-00-00-{sid}.jsonl")
    lineas = [
        {"type": "session_meta", "payload": {"id": sid, "cwd": cwd}},
        {"type": "response_item", "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "<skills_instructions>ruido"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context><cwd>x</cwd></environment_context>"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Genera el informe semanal"}]}},
        {"type": "response_item", "payload": {"type": "reasoning", "encrypted_content": "zzz"}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "shell", "arguments": json.dumps({"command": ["bash", "-lc", "make informe"]})}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "apply_patch", "arguments": json.dumps({"input": "*** Begin Patch\n*** Update File: informe/semana.md\n+hola\n*** End Patch"})}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Informe generado en informe/semana.md."}]}},
    ]
    with open(p, "w", encoding="utf-8") as fh:
        for l in lineas:
            fh.write(json.dumps(l, ensure_ascii=False) + "\n")
    if mtime:
        os.utime(p, (mtime, mtime))
    return p


def escribe_agy(home, sid="cccc3333-0000-0000-0000-000000000000", pasos=None, mtime=None, workspace=CWD):
    d = os.path.join(home, ".gemini", "antigravity-cli", "conversations")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(home, ".gemini", "antigravity-cli", "history.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"display": "x", "workspace": workspace, "conversationId": sid}) + "\n")
    p = os.path.join(d, f"{sid}.db")
    con = sqlite3.connect(p)
    con.execute("create table steps (idx integer, step_type integer, status integer, step_payload blob)")
    if pasos is None:
        pasos = [
            (0, cambiar.AGY_USER, pb({19: pb({2: "Revisa el traspaso de nómina"})})),
            (1, cambiar.AGY_ASSISTANT, pb({20: pb({7: pb({2: "run_command", 3: json.dumps({"CommandLine": "pytest -q"})})})})),
            (2, cambiar.AGY_ASSISTANT, pb({20: pb({7: pb({2: "write_to_file", 3: json.dumps({"TargetFile": "/tmp/proyecto-de-prueba/nomina.py"})})})})),
            (3, cambiar.AGY_ASSISTANT, pb({20: pb({1: "Nómina cuadrada al peso.", 3: "pensamiento interno"})})),
        ]
    con.executemany("insert into steps values (?,?,3,?)", pasos)
    con.commit()
    con.close()
    if mtime:
        os.utime(p, (mtime, mtime))
    return p


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self._home_prev = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        cambiar.DIR_CAMBIAR = os.path.join(self.home, ".cambiar")
        self._cwd_prev = os.getcwd()
        os.makedirs(CWD, exist_ok=True)
        os.chdir(CWD)

    def tearDown(self):
        os.chdir(self._cwd_prev)
        if self._home_prev is not None:
            os.environ["HOME"] = self._home_prev
        self.tmp.cleanup()

    def corre(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cambiar.main(list(args))
        return rc, out.getvalue(), err.getvalue()


class Parsers(Base):
    def test_claude(self):
        escribe_claude(self.home)
        s = cambiar.parse_claude(cambiar.sesiones_claude(CWD)[0])
        roles = [m.role for m in s.msgs]
        self.assertEqual(roles.count("user"), 3)               # encargo, traspaso ajeno, "corre las pruebas"
        self.assertNotIn("SOY UN SUBAGENTE", cambiar.volcar(s))
        self.assertNotIn("ruido", cambiar.volcar(s))
        self.assertEqual(cambiar.archivos_tocados(s.msgs), ["/tmp/proyecto-de-prueba/motores/a.py"])
        self.assertEqual(cambiar.comandos_recientes(s.msgs), ["ls motores/"])

    def test_codex(self):
        escribe_codex(self.home)
        s = cambiar.parse_codex(cambiar.sesiones_codex(CWD)[0])
        self.assertEqual([m.text for m in s.msgs if m.role == "user"], ["Genera el informe semanal"])
        self.assertEqual([m.text for m in s.msgs if m.role == "assistant"], ["Informe generado en informe/semana.md."])
        self.assertEqual(cambiar.archivos_tocados(s.msgs), ["informe/semana.md"])
        self.assertEqual(cambiar.comandos_recientes(s.msgs), ["bash -lc make informe"])

    def test_codex_filtra_por_carpeta(self):
        escribe_codex(self.home, cwd="/otra/carpeta")
        self.assertEqual(cambiar.sesiones_codex(CWD), [])
        self.assertEqual(len(cambiar.sesiones_codex(None)), 1)

    def test_agy(self):
        escribe_agy(self.home)
        s = cambiar.parse_agy(cambiar.sesiones_agy(CWD)[0])
        self.assertEqual([m.text for m in s.msgs if m.role == "user"], ["Revisa el traspaso de nómina"])
        self.assertEqual([m.text for m in s.msgs if m.role == "assistant"], ["Nómina cuadrada al peso."])
        self.assertNotIn("pensamiento interno", cambiar.volcar(s))
        self.assertEqual(cambiar.archivos_tocados(s.msgs), ["/tmp/proyecto-de-prueba/nomina.py"])
        self.assertEqual(cambiar.comandos_recientes(s.msgs), ["pytest -q"])

    def test_agy_texto_con_tilde_y_multibyte(self):
        escribe_agy(self.home, pasos=[(0, cambiar.AGY_USER, pb({19: pb({2: "Añade la columna «Año» — ¿sí?"})}))])
        s = cambiar.parse_agy(cambiar.sesiones_agy(CWD)[0])
        self.assertEqual(s.msgs[0].text, "Añade la columna «Año» — ¿sí?")


class Traspaso(Base):
    def test_contenido(self):
        escribe_claude(self.home)
        s = cambiar.parse_claude(cambiar.sesiones_claude(CWD)[0])
        t = cambiar.construir_traspaso(s, "codex")
        self.assertTrue(t.startswith(cambiar.MARCA))
        self.assertIn("Arregla el validador de ingresos", t)          # encargo = primer mensaje real
        self.assertIn("ESTADO FINAL: 3 pruebas verdes", t)           # estado = última respuesta
        self.assertIn("/tmp/proyecto-de-prueba/motores/a.py", t)
        self.assertIn("`ls motores/`", t)
        self.assertIn("cambiar --leer claude aaaa1111", t)
        self.assertIn("Destino: codex", t)

    def test_traspaso_anidado_se_omite(self):
        escribe_claude(self.home)
        s = cambiar.parse_claude(cambiar.sesiones_claude(CWD)[0])
        t = cambiar.construir_traspaso(s, "agy")
        self.assertNotIn("Session Handoff Context", t)
        self.assertIn("[traspaso previo omitido]", t)
        # el encargo no puede ser un traspaso aunque fuera el primer mensaje
        escribe_claude(self.home, sid="dddd4444-0000-0000-0000-000000000000", mensajes=[
            {"cwd": CWD, "type": "user", "message": {"role": "user", "content": TRASPASO_AJENO}},
            {"cwd": CWD, "type": "user", "message": {"role": "user", "content": "El encargo real"}},
        ])
        s2 = cambiar.parse_claude([x for x in cambiar.sesiones_claude(CWD) if x.id.startswith("dddd")][0])
        self.assertIn("## Encargo\nEl encargo real", cambiar.construir_traspaso(s2, "codex"))

    def test_techo_de_tamano(self):
        grande = "palabra " * 20000
        escribe_claude(self.home, mensajes=[
            {"cwd": CWD, "type": "user", "message": {"role": "user", "content": grande}},
            {"cwd": CWD, "type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": grande}]}},
        ] + [
            {"cwd": CWD, "type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Write", "input": {"file_path": f"/tmp/f{i}.py", "content": ""}}]}} for i in range(15)
        ])
        s = cambiar.parse_claude(cambiar.sesiones_claude(CWD)[0])
        t = cambiar.construir_traspaso(s, "codex")
        self.assertLessEqual(len(t), cambiar.MAX_CHARS)
        self.assertIn("## Estado actual", t)                        # el recorte no se come las secciones fijas
        self.assertIn("## Cómo seguir", t)
        t2 = cambiar.construir_traspaso(s, "codex", max_chars=3000)
        self.assertLessEqual(len(t2), 3000)

    def test_sin_respuesta_del_asistente(self):
        escribe_claude(self.home, mensajes=[{"cwd": CWD, "type": "user", "message": {"role": "user", "content": "Solo pregunté"}}])
        s = cambiar.parse_claude(cambiar.sesiones_claude(CWD)[0])
        self.assertIn("no tiene respuesta del asistente", cambiar.construir_traspaso(s, "agy"))


class Seleccion(Base):
    def test_mas_reciente_entre_cli(self):
        ahora = time.time()
        escribe_claude(self.home, mtime=ahora - 300)
        escribe_codex(self.home, mtime=ahora - 100)
        escribe_agy(self.home, mtime=ahora - 200)
        ses = cambiar.listar(CWD)
        self.assertEqual([s.tool for s in ses], ["codex", "agy", "claude"])
        self.assertEqual(cambiar.listar(CWD, "agy")[0].tool, "agy")

    def test_por_prefijo(self):
        escribe_claude(self.home)
        escribe_agy(self.home)
        rc, out, _ = self.corre("codex", "--sesion", "cccc", "--seco")
        self.assertEqual(rc, 0)
        self.assertIn("Origen: agy", out)

    def test_mismo_cli_no_lanza(self):
        escribe_claude(self.home)
        rc, _, err = self.corre("claude")
        self.assertEqual(rc, 4)
        self.assertIn("ya es de claude", err)


class ControlesNegativos(Base):
    def test_sin_sesiones_rc2(self):
        rc, _, err = self.corre("codex", "--seco")
        self.assertEqual(rc, 2)
        self.assertIn("No hay sesión", err)
        rc, out, _ = self.corre("--listar")
        self.assertEqual(rc, 2)

    def test_sesion_ilegible_rc3(self):
        escribe_agy(self.home, pasos=[(0, 99, b"\x00\x01\x02")])
        rc, _, err = self.corre("claude", "--seco")
        self.assertEqual(rc, 3)
        self.assertIn("no tiene mensajes legibles", err)
        rc, _, err = self.corre("--leer", "agy", "cccc")
        self.assertEqual(rc, 3)

    def test_sin_destino_rc4(self):
        rc, _, _ = self.corre()
        self.assertEqual(rc, 4)

    def test_protobuf_roto_no_revienta(self):
        self.assertEqual(cambiar.pb_texto(b"\xff\xff\xff", "1"), "")
        self.assertEqual(cambiar.pb_texto(b"", "20.1"), "")


class Procesos(Base):
    def test_fondo_sobrevive_y_se_lista(self):
        rc, out, _ = self.corre("--fondo", "sh", "-c", "echo hola; sleep 1")
        self.assertEqual(rc, 0)
        self.assertIn("Lanzado pid", out)
        rc, out, _ = self.corre("--procesos")
        self.assertIn("VIVO", out)
        time.sleep(1.5)
        rc, out, _ = self.corre("--procesos")
        self.assertIn("FIN", out)
        log = [r["log"] for r in cambiar.listar_procesos()][0]
        with open(log, encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "hola")

    def test_fondo_sin_comando_rc4(self):
        rc, _, _ = self.corre("--fondo")
        self.assertEqual(rc, 4)

    def test_zombi_ajeno_no_cuenta_como_vivo(self):
        """Un zombi cuyo padre no somos nosotros: waitpid no aplica, kill(0) responde, y solo `ps` lo delata."""
        codigo = "import os,time\npid=os.fork()\nif pid==0: os._exit(0)\nprint(pid,flush=True)\ntime.sleep(5)"
        p = subprocess.Popen([sys.executable, "-c", codigo], stdout=subprocess.PIPE, text=True)
        try:
            zpid = int(p.stdout.readline())
            time.sleep(0.3)
            self.assertFalse(cambiar._vivo(zpid))
            self.assertTrue(cambiar._vivo(p.pid))
        finally:
            p.kill()
            p.wait()


class Lanzamiento(Base):
    def test_comando_por_destino(self):
        self.assertEqual(cambiar.comando_destino("claude", "P"), ["claude", "P"])
        self.assertEqual(cambiar.comando_destino("codex", "P"), ["codex", "P"])
        self.assertEqual(cambiar.comando_destino("agy", "P"), ["agy", "-i", "P"])

    def test_lanza_con_ejecutable_falso(self):
        """Con un `codex` de mentira en el PATH, `cambiar codex` guarda el traspaso y se lo entrega entero."""
        escribe_claude(self.home)
        binf = os.path.join(self.home, "bin")
        os.makedirs(binf)
        falso = os.path.join(binf, "codex")
        with open(falso, "w") as fh:
            fh.write("#!/bin/sh\nprintf '%s' \"$1\" > \"$HOME/recibido.md\"\n")
        os.chmod(falso, 0o755)
        env = {**os.environ, "PATH": binf + os.pathsep + os.environ["PATH"], "HOME": self.home}
        r = subprocess.run([sys.executable, cambiar.__file__, "codex"], cwd=CWD, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Traspaso claude → codex", r.stderr)
        with open(os.path.join(self.home, "recibido.md"), encoding="utf-8") as fh:
            recibido = fh.read()
        self.assertTrue(recibido.startswith(cambiar.MARCA))
        self.assertIn("ESTADO FINAL", recibido)
        guardados = os.listdir(os.path.join(self.home, ".cambiar", "traspasos"))
        self.assertEqual(len(guardados), 1)
        self.assertTrue(guardados[0].endswith("-claude-a-codex.md"))


if __name__ == "__main__":
    unittest.main()
