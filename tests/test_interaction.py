"""Tests del gate de permisos de voz: categorías, descripción sin símbolos, aprobar-una-vez.

Sin audio ni TTS: se mockea la pregunta hablada (``_ask_voice``), salvo en los tests que la
cubren a ella misma (ahí se mockean ``tts.speak`` y ``ears.listen_once``).
"""

import asyncio

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from agent.voice import ears, interaction
from agent.voice.interaction import VoiceGate, _category, _describe, _program


class _Ctx:  # stand-in de ToolPermissionContext (el gate no lo usa)
    pass


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _fase_limpia():
    """La fase del ciclo de voz es proceso-global: ningún test la hereda ni la deja puesta."""
    ears._reset_fase()
    yield
    ears._reset_fase()


def test_category_agrupa_tools_afines():
    assert _category("Edit") == "edit"
    assert _category("Write") == "edit"
    assert _category("NotebookEdit") == "edit"
    assert _category("Bash") == "shell"
    assert _category("mcp__memory__write_memory") == "memory"
    assert _category("mcp__orq__optimizar") == "orq"
    assert _category("Read") == "Read"  # sin agrupar


def test_program_ignora_flags_y_rutas():
    assert _program("git commit -m x") == "git"
    assert _program("taskkill /F /IM chrome.exe") == "taskkill"
    assert _program("   ") == ""


def test_describe_no_lee_simbolos_ni_rutas():
    # Bash: nombra solo el programa; nunca el comando con símbolos.
    d = _describe("Bash", {"command": "taskkill /F /IM chrome.exe & echo $HOME"})
    assert d == "ejecutar en la terminal el comando taskkill"
    assert not any(sym in d for sym in "&$/\\")
    # Edit: frase genérica, sin la ruta ni el nombre del archivo.
    d2 = _describe("Edit", {"file_path": "C:\\x\\health_check.py"})
    assert d2 == "crear o modificar archivos en tu proyecto"
    assert "health_check" not in d2 and "\\" not in d2


def test_gate_aprueba_una_vez_por_categoria(monkeypatch):
    llamadas = []

    async def fake_ask(accion):
        llamadas.append(accion)
        return True

    monkeypatch.setattr(interaction, "_ask_voice", fake_ask)
    gate = VoiceGate()

    async def scenario():
        return [
            await gate("Edit", {"file_path": "a.py"}, _Ctx()),
            await gate("Write", {"file_path": "b.py"}, _Ctx()),
            await gate("Edit", {"file_path": "a.py"}, _Ctx()),
        ]

    resultados = _run(scenario())
    assert all(isinstance(r, PermissionResultAllow) for r in resultados)
    assert len(llamadas) == 1  # solo la 1a acción de la categoría 'edit' preguntó


def test_gate_denegar_no_recuerda(monkeypatch):
    async def fake_ask(accion):
        return False

    monkeypatch.setattr(interaction, "_ask_voice", fake_ask)
    gate = VoiceGate()
    r = _run(gate("Bash", {"command": "ls"}, _Ctx()))
    assert isinstance(r, PermissionResultDeny)
    assert "shell" not in gate.approved  # un rechazo no se memoriza como aprobado


def test_gate_reset_olvida_aprobaciones(monkeypatch):
    async def fake_ask(accion):
        return True

    monkeypatch.setattr(interaction, "_ask_voice", fake_ask)
    gate = VoiceGate()
    _run(gate("Edit", {"file_path": "a.py"}, _Ctx()))
    assert "edit" in gate.approved
    gate.reset()
    assert gate.approved == set()


# ------------------------------------------------- la pregunta hablada (_ask_voice)


def _con_respuesta(monkeypatch, respuesta, reservas=None):
    """Mockea la pregunta (TTS) y la respuesta (micrófono), anotando si estaba reservado."""

    def _speak(_texto):
        if reservas is not None:
            reservas.append(("pregunta", ears.escucha_reservada()))

    def _listen(wait_timeout=None):
        if reservas is not None:
            reservas.append(("escucha", ears.escucha_reservada()))
        return respuesta

    monkeypatch.setattr(interaction.tts, "speak", _speak)
    monkeypatch.setattr(interaction.ears, "listen_once", _listen)


def test_ask_voice_reserva_el_microfono_toda_la_pregunta(monkeypatch):
    """La F2 con la que contestas no puede leerse como barge-in contra el turno que preguntó."""
    reservas: list[tuple[str, bool]] = []
    _con_respuesta(monkeypatch, "sí", reservas)
    assert _run(interaction._ask_voice("editar archivos")) is True
    assert reservas == [("pregunta", True), ("escucha", True)]
    assert ears.escucha_reservada() is False  # y se libera al terminar


def test_ask_voice_libera_el_microfono_si_falla_la_captura(monkeypatch):
    def _explota(wait_timeout=None):
        raise OSError("micrófono ocupado")

    monkeypatch.setattr(interaction.tts, "speak", lambda _t: None)
    monkeypatch.setattr(interaction.ears, "listen_once", _explota)
    assert _run(interaction._ask_voice("editar archivos")) is False
    assert ears.escucha_reservada() is False


def test_ask_voice_deja_la_fase_en_pensando(monkeypatch):
    """El turno sigue vivo tras contestar: `listen_once` lo cierra en `inactivo` y miente."""
    _con_respuesta(monkeypatch, "sí")
    ears.publicar_captura(ears.CAPTURA_INACTIVA)  # como lo deja listen_once
    assert _run(interaction._ask_voice("editar archivos")) is True
    assert ears.fase_actual() == ears.CAPTURA_PENSANDO


def test_ask_voice_respuesta_cancelada_no_autoriza(monkeypatch):
    """Cancelar la captura con F2 llega como "" (no como None): sin sí explícito, no hay permiso."""
    _con_respuesta(monkeypatch, "")
    assert _run(interaction._ask_voice("editar archivos")) is False
