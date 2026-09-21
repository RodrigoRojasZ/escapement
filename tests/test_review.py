"""Tests del loop de aprendizaje (revisar): reviews y rejected_targets. Sin gh real."""

import pytest

from agent import ledger
from agent import orchestrator


@pytest.fixture
def ledger_tmp(tmp_path, monkeypatch):
    """Aísla el ledger en un archivo temporal (no toca el ledger real del usuario)."""
    monkeypatch.setattr(ledger.config, "LEDGER", tmp_path / "ledger.jsonl")
    return ledger


def test_latest_review_gana_el_mas_reciente(ledger_tmp):
    ledger_tmp.record({"action": "review", "pr_url": "http://x/1", "result": "pending"})
    ledger_tmp.record({"action": "review", "pr_url": "http://x/1", "result": "accepted"})
    assert ledger_tmp.latest_pr_reviews() == {"http://x/1": "accepted"}


def test_rejected_targets_detecta_rechazo(ledger_tmp):
    ledger_tmp.record({"action": "optimize", "repo": "R", "target": "a.py", "pr_url": "http://x/1"})
    ledger_tmp.record({"action": "optimize", "repo": "R", "target": "b.py", "pr_url": "http://x/2"})
    ledger_tmp.record({"action": "review", "pr_url": "http://x/1", "result": "rejected"})
    ledger_tmp.record({"action": "review", "pr_url": "http://x/2", "result": "accepted"})
    assert ledger_tmp.rejected_targets("R") == {"a.py"}


def test_rejected_targets_respeta_el_repo(ledger_tmp):
    ledger_tmp.record({"action": "optimize", "repo": "R", "target": "a.py", "pr_url": "http://x/1"})
    ledger_tmp.record({"action": "review", "pr_url": "http://x/1", "result": "rejected"})
    assert ledger_tmp.rejected_targets("OTRO") == set()


def test_review_prs_registra_cambios_sin_duplicar(ledger_tmp, monkeypatch):
    ledger_tmp.record({"action": "optimize", "repo": "R", "target": "a.py", "pr_url": "http://x/1"})

    monkeypatch.setattr(orchestrator, "pr_state", lambda repo, url: "OPEN")
    primero = orchestrator.review_prs()
    assert len(primero) == 1 and primero[0]["result"] == "pending"

    # Sin cambio de estado: no debe duplicar el evento pending.
    assert orchestrator.review_prs() == []

    # El PR se mergea: transición pending -> accepted registrada.
    monkeypatch.setattr(orchestrator, "pr_state", lambda repo, url: "MERGED")
    tercero = orchestrator.review_prs()
    assert len(tercero) == 1 and tercero[0]["result"] == "accepted"

    # Terminal: ya no se re-consulta.
    assert orchestrator.review_prs() == []


def test_ignora_marcadores_que_no_son_url(ledger_tmp, monkeypatch):
    ledger_tmp.record(
        {"action": "optimize", "repo": "R", "target": "a.py", "pr_url": "(sin PR — sin remoto)"}
    )
    monkeypatch.setattr(orchestrator, "pr_state", lambda repo, url: "OPEN")
    assert orchestrator.review_prs() == []
