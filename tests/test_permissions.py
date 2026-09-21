"""Tests del gate de texto (TextGate): categorías + aprobar-una-vez. Mock de input, sin TTY."""

import asyncio

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from agent.security.permissions import TextGate, _category, _describe


def test_describe_legible_sin_json():
    # el objetivo se resume a pocas palabras; NADA del JSON crudo ni la directiva larga.
    d = _describe(
        "mcp__orq__optimizar",
        {"repo": "R", "target": "worker/error_reviewer.py", "directiva": "Añade type hints " * 20},
    )
    assert d == "optimizar worker/error_reviewer.py"
    assert "{" not in d and "directiva" not in d and "type hints" not in d
    assert _describe("Bash", {"command": "git status -s"}) == "ejecutar: git status -s"
    assert _describe("Edit", {"file_path": "a.py"}) == "editar a.py"


class _Ctx:  # stand-in de ToolPermissionContext (el gate no lo usa)
    pass


def _run(coro):
    return asyncio.run(coro)


def test_category_agrupa():
    assert _category("Bash") == "shell"
    assert _category("Edit") == "edit"
    assert _category("mcp__orq__optimizar") == "orq"
    assert _category("mcp__memory__write_memory") == "memory"
    assert _category("Read") == "Read"  # sin agrupar


def test_textgate_aprueba_una_vez_por_categoria(monkeypatch):
    llamadas = []
    monkeypatch.setattr("builtins.input", lambda prompt="": (llamadas.append(1), "s")[1])
    gate = TextGate()

    async def scenario():
        return [
            await gate("Bash", {"command": "ls"}, _Ctx()),
            await gate("Bash", {"command": "pwd"}, _Ctx()),  # misma categoría 'shell'
            await gate("mcp__orq__optimizar", {"repo": "R"}, _Ctx()),  # otra categoría 'orq'
        ]

    r = _run(scenario())
    assert all(isinstance(x, PermissionResultAllow) for x in r)
    assert len(llamadas) == 2  # 1 pregunta por 'shell' + 1 por 'orq'; el 2º Bash no re-preguntó


def test_textgate_denegar_no_recuerda(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    gate = TextGate()
    r = _run(gate("Bash", {"command": "rm -rf /"}, _Ctx()))
    assert isinstance(r, PermissionResultDeny)
    assert "shell" not in gate.approved  # un rechazo no queda memorizado como aprobado
