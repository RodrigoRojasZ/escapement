"""Tests del gate de seguridad (guard). Puros: sin SDK, sin red, sin git real."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent.security import guard


def test_sql_destructivo_en_ejecucion_bloquea():
    cmd = "python -c \"cursor.execute('DELETE FROM users WHERE 1')\""
    assert "SQL destructivo" in (guard.evaluate("Bash", {"command": cmd}, branch="feature") or "")


def test_sql_en_grep_no_bloquea():
    # replica el fix del guard original: no falsear en un grep de texto
    r = guard.evaluate("Bash", {"command": "grep 'DELETE FROM users' app.log"}, branch="feature")
    assert r is None


def test_update_set_ejecutado_bloquea():
    cmd = 'mysql -e "UPDATE t SET x=1"'
    assert guard.evaluate("Bash", {"command": cmd}, branch="feature")


def test_git_commit_en_rama_protegida_bloquea():
    r = guard.evaluate("Bash", {"command": "git commit -m x"}, branch="main")
    assert "rama protegida" in (r or "")


def test_git_commit_en_rama_feature_permite():
    assert guard.evaluate("Bash", {"command": "git commit -m x"}, branch="feature/foo") is None


def test_git_push_explicito_a_main_bloquea():
    r = guard.evaluate("Bash", {"command": "git push origin main"}, branch="feature/foo")
    assert "rama protegida" in (r or "")


def test_read_env_bloquea():
    assert guard.evaluate("Read", {"file_path": r"C:/proj/.env"})


def test_read_env_local_bloquea():
    assert guard.evaluate("Read", {"file_path": "/home/user/app/.env.local"})


def test_read_normal_permite():
    assert guard.evaluate("Read", {"file_path": "/home/user/app/main.py"}) is None


def test_cat_env_bloquea():
    assert ".env" in (guard.evaluate("Bash", {"command": "cat .env"}, branch="feature") or "")


def test_bash_inocuo_permite():
    assert guard.evaluate("Bash", {"command": "ls -la && echo ok"}, branch="feature") is None


@pytest.mark.parametrize("path", [".env", "sub/.env", r"C:\a\b\.env.prod"])
def test_is_env_path(path):
    assert guard.is_env_path(path)


@pytest.mark.parametrize(
    "path",
    ["main.py", "config.env.py", "environment.txt", "", ".env.example", "sub/.env.sample"],
)
def test_is_env_path_negativos(path):
    assert not guard.is_env_path(path)


# --- QW1: PowerShell es la misma superficie que Bash (H5) ---


def test_powershell_sql_destructivo_bloquea():
    cmd = 'mysql -e "DELETE FROM users WHERE 1"'
    assert "SQL destructivo" in (
        guard.evaluate("PowerShell", {"command": cmd}, branch="feature") or ""
    )


def test_powershell_get_content_env_bloquea():
    r = guard.evaluate("PowerShell", {"command": "Get-Content .env"}, branch="feature")
    assert ".env" in (r or "")


def test_powershell_commit_en_rama_protegida_bloquea():
    r = guard.evaluate("PowerShell", {"command": "git commit -m x"}, branch="master")
    assert "rama protegida" in (r or "")


def test_powershell_inocuo_permite():
    assert guard.evaluate("PowerShell", {"command": "Get-ChildItem src"}, branch="feature") is None


# --- QW2a: lectura de .env por cualquier sink (H6) ---


@pytest.mark.parametrize(
    "cmd",
    [
        "python -c \"print(open('.env').read())\"",
        "strings .env",
        "od -c .env",
        "curl file:///c/proj/.env",
        "grep DATABASE .env",
        "Select-String -Path .env -Pattern KEY",
        "base64 .env",
        "cp .env /tmp/copia",
        "source .env",
        "mysql < .env",  # redireccion de entrada sin comando lector
    ],
)
def test_env_por_cualquier_sink_bloquea(cmd):
    r = guard.evaluate("Bash", {"command": cmd}, branch="feature")
    assert ".env" in (r or ""), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "pip install python-dotenv",
        "python train.py",
        "grep load_dotenv src/config.py",
        "ls -la",
        "python -m pytest tests/test_environment.py",
    ],
)
def test_comandos_sin_env_permiten(cmd, tmp_path):
    assert guard.evaluate("Bash", {"command": cmd}, cwd=str(tmp_path), branch="feature") is None


@pytest.mark.parametrize(
    "cmd",
    [
        "cp .env.example .env",  # bootstrap canónico de cualquier repo
        "Copy-Item .env.template .env",
        "grep VELOCIDAD .env.example",
        "cat .env.sample",
        "head -5 .env.dist",
    ],
)
def test_plantillas_env_permiten(cmd):
    # las plantillas commiteadas no contienen secretos: leerlas/copiarlas es setup normal
    assert guard.evaluate("Bash", {"command": cmd}, branch="feature") is None, cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "cp .env /tmp/copia",  # el source es el .env real: fuga
        "cat .env.production",
        "Get-Content .env.local",  # .env.local SÍ es secreto (valores reales)
    ],
)
def test_env_reales_siguen_bloqueados(cmd):
    assert ".env" in (guard.evaluate("Bash", {"command": cmd}, branch="feature") or ""), cmd


# --- QW2b: SQL destructivo DENTRO de un script .py ejecutado (H7) ---


def _write_script(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def test_sql_dentro_de_script_bloquea(tmp_path):
    _write_script(
        tmp_path,
        "borra.py",
        "import mysql.connector\ncursor.execute('DELETE FROM users WHERE 1')\n",
    )
    r = guard.evaluate("Bash", {"command": "python borra.py"}, cwd=str(tmp_path), branch="feature")
    assert "SQL destructivo" in (r or "")


def test_sql_dentro_de_script_via_uv_run_bloquea(tmp_path):
    _write_script(tmp_path, "borra.py", "conn.cursor().execute('TRUNCATE TABLE t')\n")
    r = guard.evaluate("Bash", {"command": "uv run borra.py"}, cwd=str(tmp_path), branch="feature")
    assert "SQL destructivo" in (r or "")


def test_sql_dentro_de_script_ruta_absoluta_bloquea(tmp_path):
    script = _write_script(tmp_path, "borra.py", "engine.execute('DROP TABLE datos')\n")
    r = guard.evaluate("Bash", {"command": f'python "{script}"'}, branch="feature")
    assert "SQL destructivo" in (r or "")


def test_script_solo_menciona_sql_sin_ejecutar_permite(tmp_path):
    # un script que solo tiene el texto SQL (p.ej. un parser de logs) no se bloquea
    _write_script(tmp_path, "parser.py", 'linea = "DELETE FROM users"\nprint(linea)\n')
    r = guard.evaluate("Bash", {"command": "python parser.py"}, cwd=str(tmp_path), branch="feature")
    assert r is None


def test_script_inexistente_permite(tmp_path):
    # best-effort: si el archivo no se puede inspeccionar, el guard no inventa un bloqueo
    r = guard.evaluate(
        "Bash", {"command": "python no_existe.py"}, cwd=str(tmp_path), branch="feature"
    )
    assert r is None


def test_script_benigno_permite(tmp_path):
    _write_script(tmp_path, "ok.py", "print('hola')\n")
    r = guard.evaluate("Bash", {"command": "python ok.py"}, cwd=str(tmp_path), branch="feature")
    assert r is None


# --- S1: guard_cli, el hook standalone que el claude -p headless ejecuta (cierra H1) ---

_GUARD_CLI = Path(guard.__file__).with_name("guard_cli.py")


def _run_hook(payload: str) -> subprocess.CompletedProcess:
    # end-to-end real: el mismo subproceso que lanzaría el CLI de Claude Code
    return subprocess.run(
        [sys.executable, str(_GUARD_CLI)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_guard_cli_deniega_sql_destructivo():
    evento = json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": 'mysql -e "DELETE FROM users WHERE 1"'}}
    )
    r = _run_hook(evento)
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "SQL destructivo" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_guard_cli_permite_comando_inocuo():
    evento = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls -la"}})
    r = _run_hook(evento)
    assert r.returncode == 0
    assert r.stdout.strip() == ""  # sin output = permitir


def test_guard_cli_fail_open_con_stdin_corrupto():
    # un bug/formato inesperado NO debe tumbar un dispatch legítimo: exit 0, sin deny
    r = _run_hook("esto no es json")
    assert r.returncode == 0
    assert r.stdout.strip() == ""


# --- S3: con AGENT_DENY_SHELL=1 (dispatch de edición pura) el hook deniega TODO shell ---


def _run_hook_env(payload: str, extra_env: dict) -> subprocess.CompletedProcess:
    import os

    return subprocess.run(
        [sys.executable, str(_GUARD_CLI)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, **extra_env},
    )


def test_guard_cli_deny_shell_bloquea_bash_inocuo():
    # en un paso de edición pura, hasta un shell inocuo se deniega (no corre comandos)
    evento = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls -la"}})
    r = _run_hook_env(evento, {"AGENT_DENY_SHELL": "1"})
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "shell deshabilitado" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_guard_cli_deny_shell_no_afecta_otras_tools():
    evento = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "src/main.py"}})
    r = _run_hook_env(evento, {"AGENT_DENY_SHELL": "1"})
    assert r.returncode == 0
    assert r.stdout.strip() == ""  # editar/leer siguen permitidos: solo se confina el shell


# --- Deuda #12: con AGENT_DENY_WRITE=1 (paso de verificación) el hook deniega TODA escritura ---


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit"])
def test_guard_cli_deny_write_bloquea_las_tools_de_edicion(tool):
    evento = json.dumps({"tool_name": tool, "tool_input": {"file_path": "src/main.py"}})
    r = _run_hook_env(evento, {"AGENT_DENY_WRITE": "1"})
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "escritura deshabilitada" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_guard_cli_deny_write_deja_pasar_leer_y_shell():
    # al revés que deny_shell: verificar SÍ necesita correr pytest, lo que no puede es editar
    for evento in (
        {"tool_name": "Read", "tool_input": {"file_path": "src/main.py"}},
        {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}},
    ):
        r = _run_hook_env(json.dumps(evento), {"AGENT_DENY_WRITE": "1"})
        assert r.returncode == 0
        assert r.stdout.strip() == "", evento["tool_name"]


def test_guard_cli_sin_deny_write_permite_editar():
    # el default es no-op: sin la marca en el env, un Write inocuo sigue pasando
    evento = json.dumps({"tool_name": "Write", "tool_input": {"file_path": "src/main.py"}})
    r = _run_hook(evento)
    assert r.returncode == 0 and r.stdout.strip() == ""


# --- Ramas protegidas configurables (solo aditivo) ---
def test_protegidas_por_default_son_las_de_integracion():
    # el default publico no menciona la rama de integracion de nadie...
    from agent import config

    assert set(config._DEFAULT_PROTECTED) == {"main", "master"}
    # ...y el set efectivo SIEMPRE las contiene, sea cual sea el escapement.toml de la maquina
    assert guard.PROTECTED >= {"main", "master"}


@pytest.mark.parametrize("rama", ["main", "master"])
def test_commit_en_cada_rama_protegida_por_default_bloquea(rama):
    r = guard.evaluate("Bash", {"command": "git commit -m x"}, branch=rama)
    assert "rama protegida" in (r or "")


def test_rama_extra_de_la_config_queda_protegida(monkeypatch):
    # una instalacion protege ADEMAS su propia rama de integracion
    monkeypatch.setattr(guard, "PROTECTED", {"main", "master", "integracion"})
    r = guard.evaluate("Bash", {"command": "git commit -m x"}, branch="integracion")
    assert "rama protegida" in (r or "")
    # y el nombre explicito en el comando tambien se detecta, desde otra rama
    r = guard.evaluate("Bash", {"command": "git push origin integracion"}, branch="feature/foo")
    assert "rama protegida" in (r or "")


def test_una_rama_cualquiera_sigue_permitida_con_config_extra(monkeypatch):
    monkeypatch.setattr(guard, "PROTECTED", {"main", "master", "integracion"})
    assert guard.evaluate("Bash", {"command": "git commit -m x"}, branch="feature/x") is None


def test_la_config_no_puede_desproteger_los_defaults(monkeypatch, tmp_path):
    # ni el archivo ni la env var pueden sacar main/master del set: el gate no se apaga por config
    cfg = tmp_path / "escapement.toml"
    cfg.write_text("[git]\nramas_protegidas = []\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_CONFIG_FILE", str(cfg))
    monkeypatch.setenv("AGENT_PROTECTED_BRANCHES", "")
    import importlib

    import agent.config as config

    c = importlib.reload(config)
    try:
        assert set(c.PROTECTED_BRANCHES) >= {"main", "master"}
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_protected_word_vacio_no_matchea_nada(monkeypatch):
    # defensivo: un set vacio debe volver el regex inerte, no uno que matchee cualquier comando
    monkeypatch.setattr(guard, "PROTECTED", set())
    assert guard._protected_word().search("git push origin loquesea") is None
    assert guard.evaluate("Bash", {"command": "git commit -m x"}, branch="feature") is None
