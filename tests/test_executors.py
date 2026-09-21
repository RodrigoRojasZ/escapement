"""Tests del executor pluggable (registry + builders). Sin correr binarios ni cuota."""

import json

import pytest

from agent import config
from agent.executors import EXECUTORS, resolve


@pytest.fixture(autouse=True)
def data_dir_tmp(tmp_path, monkeypatch):
    """Aísla DATA_DIR: el builder de claude escribe guard_settings.json ahí (S1)."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")


def test_registry_tiene_los_motores():
    assert set(EXECUTORS) >= {"claude", "antigravity", "cursor"}


def test_claude_edita_por_stdin_con_skip_permissions():
    cmd, stdin = EXECUTORS["claude"].build_edit("claude", "haz X")
    assert cmd[:3] == ["claude", "-p", "--dangerously-skip-permissions"]
    assert stdin == "haz X"  # prompt por stdin


# --- S1: el dispatch headless lleva el guard como hook PreToolUse (cierra H1) ---


def test_claude_edit_inyecta_settings_con_el_guard(tmp_path):
    cmd, _ = EXECUTORS["claude"].build_edit("claude", "haz X")
    assert "--settings" in cmd
    settings_path = cmd[cmd.index("--settings") + 1]
    settings = json.loads(open(settings_path, encoding="utf-8").read())
    hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]
    assert hook["type"] == "command"
    assert "guard_cli.py" in hook["command"]  # el CLI ejecuta el guard antes de cada tool


def test_claude_read_no_lleva_settings():
    # el juez (read) no recibe permisos de edición: no necesita el hook
    cmd, _ = EXECUTORS["claude"].build_read("claude", "juzga")
    assert "--settings" not in cmd


def test_antigravity_edita_por_arg_con_approve_all():
    cmd, stdin = EXECUTORS["antigravity"].build_edit("agy", "haz X")
    assert cmd == ["agy", "-p", "haz X", "--headless", "--approve", "all"]
    assert stdin is None  # prompt embebido como argumento


def test_read_mode_no_aprueba_escrituras():
    # el modo 'read' (juez) no lleva flags de auto-aprobación de escrituras
    cmd_agy, _ = EXECUTORS["antigravity"].build_read("agy", "juzga")
    assert "--approve" not in cmd_agy
    cmd_claude, _ = EXECUTORS["claude"].build_read("claude", "juzga")
    assert "--dangerously-skip-permissions" not in cmd_claude


def test_executor_model_flag_default_noop():
    # sin AGENT_EXECUTOR_MODEL, el cmd no lleva --model (comportamiento previo intacto)
    assert (
        config.EXECUTOR_MODEL == ""
        or "--model" not in EXECUTORS["claude"].build_edit("claude", "x")[0]
    )


def test_executor_model_flag_forzado(monkeypatch):
    # con AGENT_EXECUTOR_MODEL seteado, TODO dispatch claude (edit y read) lleva --model <id>
    monkeypatch.setattr(config, "EXECUTOR_MODEL", "claude-opus-5")
    cmd_edit, _ = EXECUTORS["claude"].build_edit("claude", "x")
    cmd_read, _ = EXECUTORS["claude"].build_read("claude", "x")
    assert cmd_edit[-2:] == ["--model", "claude-opus-5"]
    assert cmd_read[-2:] == ["--model", "claude-opus-5"]


def test_model_explicito_gana_sobre_executor_model(monkeypatch):
    # el tiering por tarea (model explícito) pisa el override global AGENT_EXECUTOR_MODEL
    monkeypatch.setattr(config, "EXECUTOR_MODEL", "claude-global")
    cmd_edit, _ = EXECUTORS["claude"].build_edit("claude", "x", model="claude-tarea")
    cmd_read, _ = EXECUTORS["claude"].build_read("claude", "x", model="claude-tarea")
    assert cmd_edit[-2:] == ["--model", "claude-tarea"]
    assert cmd_read[-2:] == ["--model", "claude-tarea"]


def test_model_vacio_cae_al_executor_model(monkeypatch):
    # model="" (tiering OFF) no anula el override global: sigue usando EXECUTOR_MODEL
    monkeypatch.setattr(config, "EXECUTOR_MODEL", "claude-global")
    cmd, _ = EXECUTORS["claude"].build_edit("claude", "x", model="")
    assert cmd[-2:] == ["--model", "claude-global"]


def test_model_vacio_y_sin_executor_model_es_noop(monkeypatch):
    monkeypatch.setattr(config, "EXECUTOR_MODEL", "")
    cmd, _ = EXECUTORS["claude"].build_edit("claude", "x", model="")
    assert "--model" not in cmd


def test_builders_sin_seleccion_de_modelo_ignoran_model():
    # agy/cursor no exponen --model: aceptan el kwarg sin alterar el argv (limitación documentada)
    assert EXECUTORS["antigravity"].build_edit("agy", "x", model="m") == EXECUTORS[
        "antigravity"
    ].build_edit("agy", "x")
    assert EXECUTORS["cursor"].build_read("cursor-agent", "x", model="m") == EXECUTORS[
        "cursor"
    ].build_read("cursor-agent", "x")


def test_run_agent_propaga_model_al_builder(monkeypatch, tmp_path):
    from agent import executors

    capturado = {}

    def _fake_run(*a, **k):
        capturado["cmd"] = a[0] if a else k.get("args")
        return _FakeRun()

    monkeypatch.setattr(config, "EXECUTOR_MODEL", "")
    monkeypatch.setattr(executors.subprocess, "run", _fake_run)
    executors.run_agent("edita X", cwd=tmp_path, mode="edit", model="claude-tarea")
    assert capturado["cmd"][-2:] == ["--model", "claude-tarea"]


def test_resolve_por_config(monkeypatch):
    monkeypatch.setattr(config, "EXECUTOR", "claude")
    assert resolve().name == "claude"
    monkeypatch.setattr(config, "EXECUTOR", "antigravity")
    assert resolve().name == "antigravity"


def test_resolve_desconocido_da_error_claro(monkeypatch):
    monkeypatch.setattr(config, "EXECUTOR", "inexistente")
    with pytest.raises(ValueError, match="desconocido"):
        resolve()


# --- QW3: pre-flight de secretos en el sink (run_agent), no solo en optimize ---

_PROMPT_CON_SECRETO = 'Refactoriza esto:\naws_id = "AKIAABCDEFGHIJKLMNOP"\n'


def _no_dispatch(*a, **k):
    raise AssertionError("no debio despachar: el pre-flight tenia que abortar antes")


class _FakeRun:
    returncode = 0
    stdout = "ok"
    stderr = ""


def test_run_agent_edit_bloquea_prompt_con_secretos(monkeypatch, tmp_path):
    from agent import executors

    monkeypatch.setattr(executors.subprocess, "run", _no_dispatch)
    ok, out = executors.run_agent(_PROMPT_CON_SECRETO, cwd=tmp_path, mode="edit")
    assert ok is False
    assert "pre-flight" in out and "aws-key" in out


def test_run_agent_edit_allow_secrets_fuerza_dispatch(monkeypatch, tmp_path):
    from agent import executors

    monkeypatch.setattr(executors.subprocess, "run", lambda *a, **k: _FakeRun())
    ok, out = executors.run_agent(
        _PROMPT_CON_SECRETO, cwd=tmp_path, mode="edit", allow_secrets=True
    )
    assert ok is True and out == "ok"


def test_run_agent_read_no_escanea(monkeypatch, tmp_path):
    # el juez (mode="read") no aplica el pre-flight: su prompt lo arma el orquestador
    from agent import executors

    monkeypatch.setattr(executors.subprocess, "run", lambda *a, **k: _FakeRun())
    ok, _ = executors.run_agent(_PROMPT_CON_SECRETO, cwd=tmp_path, mode="read")
    assert ok is True


def test_run_agent_edit_prompt_limpio_despacha(monkeypatch, tmp_path):
    from agent import executors

    monkeypatch.setattr(executors.subprocess, "run", lambda *a, **k: _FakeRun())
    ok, out = executors.run_agent("agrega type hints a utils.py", cwd=tmp_path, mode="edit")
    assert ok is True and out == "ok"


# --- S3: no_shell confina el dispatch de edición pura (sin Bash/PowerShell) (H3) ---


def test_claude_edit_no_shell_agrega_disallowed_tools():
    cmd, _ = EXECUTORS["claude"].build_edit("claude", "x", no_shell=True)
    i = cmd.index("--disallowedTools")
    assert cmd[i + 1 : i + 3] == ["Bash", "PowerShell"]


def test_claude_edit_default_no_confina():
    # no_shell=False (default) es no-op: comportamiento previo intacto
    cmd, _ = EXECUTORS["claude"].build_edit("claude", "x")
    assert "--disallowedTools" not in cmd


def test_builders_sin_deny_ignoran_no_shell():
    # agy/cursor no tienen deny de tools: aceptan el kwarg sin alterar el argv (limitación documentada)
    assert EXECUTORS["antigravity"].build_edit("agy", "x", no_shell=True) == EXECUTORS[
        "antigravity"
    ].build_edit("agy", "x")
    assert EXECUTORS["cursor"].build_edit("cursor-agent", "x", no_shell=True) == EXECUTORS[
        "cursor"
    ].build_edit("cursor-agent", "x")


def test_run_agent_no_shell_marca_el_env_del_subproceso(monkeypatch, tmp_path):
    from agent import executors

    capturado = {}

    def _fake_run(*a, **k):
        capturado.update(k)
        return _FakeRun()

    monkeypatch.setattr(executors.subprocess, "run", _fake_run)
    ok, _ = executors.run_agent("edita X", cwd=tmp_path, mode="edit", no_shell=True)
    assert ok is True
    assert capturado["env"][executors.DENY_SHELL_ENV] == "1"  # el hook del guard lo hereda


def test_run_agent_sin_no_shell_no_toca_el_env(monkeypatch, tmp_path):
    from agent import executors

    capturado = {}

    def _fake_run(*a, **k):
        capturado.update(k)
        return _FakeRun()

    monkeypatch.setattr(executors.subprocess, "run", _fake_run)
    executors.run_agent("edita X", cwd=tmp_path, mode="edit")
    assert capturado["env"] is None  # default: hereda el env del padre tal cual


# --- Techo de tiempo del dispatch (AGENT_EXECUTOR_TIMEOUT) ---


def test_el_timeout_por_default_sale_de_config(monkeypatch):
    """`run_agent` no lleva el reloj hardcodeado: lo toma de `config.EXECUTOR_TIMEOUT`."""
    import inspect

    from agent.executors import run_agent

    default = inspect.signature(run_agent).parameters["timeout"].default
    assert default == config.EXECUTOR_TIMEOUT
    assert config.EXECUTOR_TIMEOUT >= 5400  # el default de hoy; el env puede subirlo


def test_el_timeout_llega_al_subprocess(monkeypatch):
    """El valor viaja hasta `subprocess.run`, que es donde el reloj muerde de verdad."""
    import subprocess as sp

    from agent import executors

    visto = {}

    class _R:
        returncode = 0
        stdout = "listo"
        stderr = ""

    def _fake_run(cmd, **kw):
        visto.update(kw)
        return _R()

    monkeypatch.setattr(executors.subprocess, "run", _fake_run)
    monkeypatch.setattr(executors.shutil, "which", lambda _n: "claude")
    ok, _out = executors.run_agent("haz X", cwd=".", timeout=1234)
    assert ok
    assert visto["timeout"] == 1234

    executors.run_agent("haz X", cwd=".")
    assert visto["timeout"] == config.EXECUTOR_TIMEOUT
    assert sp is not None  # el módulo real no quedó parcheado fuera del test
