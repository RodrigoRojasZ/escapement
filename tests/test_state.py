"""Tests de la cola persistente (state.json): mutadores serializados bajo concurrencia (S4/H8).

Sin red ni executor: solo el archivo de estado en un tmp_path. El punto es que dos escritores
concurrentes (daemon de voz + REPL + orquestador) no se pierdan actualizaciones entre el
load y el save.
"""

import threading

import pytest

from agent import state


@pytest.fixture
def state_tmp(tmp_path, monkeypatch):
    """Aísla state.json (y su .lock) en un archivo temporal."""
    monkeypatch.setattr(state, "STATE", tmp_path / "state.json")
    return state


def test_enqueue_concurrente_no_pierde_tareas(state_tmp):
    # 2 hilos x 20 enqueues de 1 tarea: sin lock, el load-modify-save intercalado pierde
    # actualizaciones (last-writer-wins); con el lock quedan las 40, siempre.
    def encola(prefijo):
        for i in range(20):
            state_tmp.enqueue([{"repo": "R", "target": f"{prefijo}-{i}.py", "directiva": "x"}])

    hilos = [threading.Thread(target=encola, args=(p,)) for p in ("a", "b")]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    cola = state_tmp._load()["queue"]
    assert len(cola) == 40
    assert state_tmp._load()["seq"] == 40
    assert len({t["id"] for t in cola}) == 40  # ids únicos: el contador no se pisó


def test_mark_concurrente_no_pierde_estados(state_tmp):
    state_tmp.enqueue([{"repo": "R", "target": f"t{i}.py", "directiva": "x"} for i in range(20)])
    ids = [t["id"] for t in state_tmp.pending()]

    def marca(mis_ids):
        for tid in mis_ids:
            state_tmp.mark(tid, "seguro")

    hilos = [
        threading.Thread(target=marca, args=(ids[:10],)),
        threading.Thread(target=marca, args=(ids[10:],)),
    ]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    assert all(t["status"] == "seguro" for t in state_tmp._load()["queue"])


def test_set_throttled_es_visible(state_tmp):
    state_tmp.set_throttled("2026-07-08T12:00:00")
    assert state_tmp.throttled_until() == "2026-07-08T12:00:00"
    state_tmp.set_throttled(None)
    assert state_tmp.throttled_until() is None
