"""Tests de ledger.stats: agregación de observabilidad (Fase 0)."""

import pytest

from agent import ledger


@pytest.fixture
def ledger_tmp(tmp_path, monkeypatch):
    """Aísla el ledger en un archivo temporal (no toca el ledger real del usuario)."""
    monkeypatch.setattr(ledger.config, "LEDGER", tmp_path / "ledger.jsonl")
    return ledger


def test_stats_ledger_vacio(ledger_tmp):
    st = ledger_tmp.stats()
    assert st == {
        "total": 0,
        "verificadas": 0,
        "tasa_exito": 0.0,
        "bloqueadas_secretos": 0,
        "duracion_prom_s": 0.0,
        "por_executor": {},
    }


def test_stats_filtra_por_action(ledger_tmp):
    ledger_tmp.record({"action": "review", "pr_url": "http://x/1", "result": "accepted"})
    ledger_tmp.record(
        {"action": "optimize", "verified": True, "executor": "claude", "duration_s": 10}
    )
    st = ledger_tmp.stats()  # default action="optimize" -> ignora el review
    assert st["total"] == 1
    assert st["verificadas"] == 1


def test_stats_tasa_exito_y_bloqueadas(ledger_tmp):
    ledger_tmp.record(
        {"action": "optimize", "verified": True, "executor": "claude", "duration_s": 4}
    )
    ledger_tmp.record(
        {"action": "optimize", "verified": False, "executor": "claude", "duration_s": 6}
    )
    ledger_tmp.record(
        {"action": "optimize", "verified": False, "blocked_secrets": 2, "executor": "claude"}
    )
    st = ledger_tmp.stats()
    assert st["total"] == 3
    assert st["verificadas"] == 1
    assert st["tasa_exito"] == round(1 / 3, 2)
    assert st["bloqueadas_secretos"] == 1  # cuenta eventos, no la suma de secretos


def test_stats_duracion_promedio_ignora_faltantes(ledger_tmp):
    ledger_tmp.record({"action": "optimize", "verified": True, "duration_s": 10})
    ledger_tmp.record({"action": "optimize", "verified": True, "duration_s": 20})
    ledger_tmp.record({"action": "optimize", "verified": True})  # sin duration_s
    assert ledger_tmp.stats()["duracion_prom_s"] == 15.0


def test_read_tolera_linea_truncada(ledger_tmp, tmp_path):
    # un append interrumpido deja una linea JSON a medias: read() la salta, no muere
    ledger_tmp.record({"action": "optimize", "verified": True})
    with (tmp_path / "ledger.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"action": "optimi')  # torn write
    eventos = ledger_tmp.read()
    assert len(eventos) == 1
    assert eventos[0]["action"] == "optimize"


def test_record_tras_linea_torn_no_se_contamina(ledger_tmp, tmp_path):
    # una linea torn SIN \n final no debe tragarse el siguiente evento valido: record()
    # cierra la linea a medias antes de anexar el suyo.
    ledger_tmp.record({"action": "optimize", "verified": True})
    with (tmp_path / "ledger.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"action": "optimi')  # torn, sin newline
    ledger_tmp.record({"action": "optimize", "verified": False})
    eventos = ledger_tmp.read()
    assert len(eventos) == 2  # el evento nuevo sobrevive; solo la torn se pierde
    assert [e["verified"] for e in eventos] == [True, False]


def test_stats_por_executor(ledger_tmp):
    ledger_tmp.record({"action": "optimize", "verified": True, "executor": "claude"})
    ledger_tmp.record({"action": "optimize", "verified": True, "executor": "antigravity"})
    ledger_tmp.record({"action": "optimize", "verified": False, "executor": "claude"})
    ledger_tmp.record({"action": "optimize", "verified": False})  # sin executor -> "?"
    por = ledger_tmp.stats()["por_executor"]
    assert por == {"claude": 2, "antigravity": 1, "?": 1}
