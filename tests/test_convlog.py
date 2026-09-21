"""Tests del registro de conversaciones (convlog): record + read, ruta aislada."""

import pytest

from agent import convlog


@pytest.fixture
def conv_tmp(tmp_path, monkeypatch):
    """Aísla el registro en un archivo temporal (no toca el del usuario)."""
    monkeypatch.setattr(convlog.config, "CONVERSATIONS", tmp_path / "conversations.jsonl")
    return convlog


def test_record_y_read_round_trip(conv_tmp):
    conv_tmp.record("hola", "qué tal", backend="local")
    conv_tmp.record("edita x", "hecho", backend="claude", session_id="s1", duration_s=2.5)
    turnos = conv_tmp.read()
    assert len(turnos) == 2
    assert turnos[0]["user"] == "hola" and turnos[0]["backend"] == "local"
    assert turnos[0]["session_id"] is None  # los turnos locales no tienen sesión del SDK
    assert turnos[1]["reply"] == "hecho" and turnos[1]["session_id"] == "s1"
    assert turnos[1]["duration_s"] == 2.5
    assert all("ts" in t for t in turnos)


def test_read_limit_devuelve_los_ultimos(conv_tmp):
    for i in range(5):
        conv_tmp.record(f"u{i}", f"r{i}", backend="local")
    assert [t["user"] for t in conv_tmp.read(limit=2)] == ["u3", "u4"]


def test_read_sin_archivo_es_vacio(conv_tmp):
    assert conv_tmp.read() == []
