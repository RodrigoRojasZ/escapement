"""Tests aislados del bus de eventos (agent.bus): pub/sub in-process + journal JSONL.

Sin red, sin GPU, sin Claude: el journal se redirige a tmp_path y los suscriptores son
callables locales. Cubre el contrato público (publish/subscribe/read) y el cableado de
convlog.record -> evento ``turn``.
"""

from __future__ import annotations

import json
import threading

import pytest

from agent import bus, config, convlog


@pytest.fixture(autouse=True)
def _aislar(monkeypatch, tmp_path):
    """Journal en tmp y bus sin suscriptores heredados de otros tests."""
    monkeypatch.setattr(config, "EVENTS", tmp_path / "events.jsonl")
    monkeypatch.setattr(config, "CONVERSATIONS", tmp_path / "conversations.jsonl")
    bus._reset()
    yield
    bus._reset()


# ---------------------------------------------------------------- publish + subscribe


def test_publish_entrega_evento_al_suscriptor():
    visto = []
    bus.subscribe("plan.", visto.append)
    ev = bus.publish("plan.step", {"id": 1}, source="runner")
    assert [e.topic for e in visto] == ["plan.step"]
    assert visto[0].data == {"id": 1}
    assert visto[0].source == "runner"
    assert visto[0].ts  # timestamp asignado
    assert ev is visto[0]


def test_prefijo_filtra_y_cadena_vacia_recibe_todo():
    planes, todos = [], []
    bus.subscribe("plan.", planes.append)
    bus.subscribe("", todos.append)
    bus.publish("plan.step")
    bus.publish("voice.state")
    assert [e.topic for e in planes] == ["plan.step"]
    assert [e.topic for e in todos] == ["plan.step", "voice.state"]


def test_unsubscribe_es_idempotente():
    visto = []
    cancelar = bus.subscribe("", visto.append)
    bus.publish("a")
    cancelar()
    cancelar()  # segunda llamada: no-op, no lanza
    bus.publish("b")
    assert [e.topic for e in visto] == ["a"]


def test_suscriptor_roto_no_afecta_al_productor_ni_al_resto():
    visto = []

    def roto(_ev):
        raise RuntimeError("boom")

    bus.subscribe("", roto)
    bus.subscribe("", visto.append)
    bus.publish("x")  # no lanza
    assert [e.topic for e in visto] == ["x"]


def test_data_none_equivale_a_dict_vacio_y_se_copia():
    original = {"k": 1}
    ev1 = bus.publish("t", None)
    ev2 = bus.publish("t", original)
    original["k"] = 2  # mutar el dict del caller no afecta al evento ya publicado
    assert ev1.data == {}
    assert ev2.data == {"k": 1}


# ---------------------------------------------------------------- journal (events.jsonl)


def test_journal_true_persiste_linea_jsonl():
    bus.publish("plan.done", {"completado": True}, source="runner")
    lineas = config.EVENTS.read_text(encoding="utf-8").splitlines()
    assert len(lineas) == 1
    fila = json.loads(lineas[0])
    assert fila["topic"] == "plan.done"
    assert fila["data"] == {"completado": True}
    assert fila["source"] == "runner"


def test_journal_false_no_escribe_pero_si_notifica():
    visto = []
    bus.subscribe("", visto.append)
    bus.publish("turn", {"user": "hola"}, journal=False)
    assert not config.EVENTS.exists()
    assert [e.topic for e in visto] == ["turn"]


def test_journal_ilegible_no_tumba_al_productor(monkeypatch, tmp_path):
    # EVENTS apunta a un directorio -> open("a") falla; publish debe tragarlo y aun notificar.
    monkeypatch.setattr(config, "EVENTS", tmp_path)
    visto = []
    bus.subscribe("", visto.append)
    bus.publish("x")
    assert [e.topic for e in visto] == ["x"]


def test_read_filtra_por_prefijo_y_limita():
    bus.publish("plan.step", {"id": 1})
    bus.publish("voice.state", {"state": "activo"})
    bus.publish("plan.step", {"id": 2})
    assert bus.read() == bus.read(topic="")
    assert [e["data"]["id"] for e in bus.read(topic="plan.")] == [1, 2]
    assert [e["data"]["id"] for e in bus.read(limit=1, topic="plan.")] == [2]
    assert bus.read(topic="nada.") == []


def test_read_sin_journal_devuelve_lista_vacia():
    assert bus.read() == []


# ---------------------------------------------------------------- concurrencia básica


def test_publish_concurrente_no_pierde_eventos():
    visto = []
    lock = threading.Lock()

    def acumular(ev):
        with lock:
            visto.append(ev)

    bus.subscribe("", acumular)
    hilos = [
        threading.Thread(target=lambda i=i: bus.publish("t", {"i": i}, journal=False))
        for i in range(20)
    ]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    assert sorted(e.data["i"] for e in visto) == list(range(20))


# ---------------------------------------------------------------- cableado convlog -> turn


def test_convlog_record_publica_turn_sin_journal():
    visto = []
    bus.subscribe("turn", visto.append)
    convlog.record("hola", "qué tal", backend="local", session_id=None)
    assert len(visto) == 1
    assert visto[0].data == {
        "backend": "local",
        "session_id": None,
        "user": "hola",
        "reply": "qué tal",
    }
    assert visto[0].source == "local"
    # El texto ya es durable en conversations.jsonl; el bus no lo duplica en events.jsonl.
    assert config.CONVERSATIONS.exists()
    assert not config.EVENTS.exists()


def test_convlog_record_sobrevive_suscriptor_roto():
    def roto(_ev):
        raise RuntimeError("boom")

    bus.subscribe("turn", roto)
    convlog.record("a", "b", backend="claude", session_id="s1")  # no lanza
    assert len(convlog.read()) == 1
