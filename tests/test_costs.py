"""Tests del cost tracker (R3): estimación de tokens, budget y el observer sobre run_agent.

Aislados: el único dispatch se simula monkeypatcheando subprocess.run (sin lanzar el executor).
"""

import types

from agent import costs, executors


# --- estimate_tokens -----------------------------------------------------------


def test_estimate_tokens_proxy_4_chars():
    assert costs.estimate_tokens("") == 0
    assert costs.estimate_tokens("abcd") == 1  # (4+3)//4
    assert costs.estimate_tokens("a" * 8) == 2
    assert costs.estimate_tokens(None) == 0  # tolera None


# --- PlanCosts -----------------------------------------------------------------


def test_plancosts_acumula_y_total():
    c = costs.PlanCosts("obj")
    c.add("abcd", "abcdefgh")  # in=1, out=2
    c.add("abcd", "")  # in=1, out=0
    assert c.dispatches == 2 and c.est_in == 2 and c.est_out == 2
    assert c.total == 4


def test_over_budget_sin_budget_es_falso():
    c = costs.PlanCosts("obj")  # budget None
    c.add("x" * 400, "y" * 400)
    assert c.over_budget() is False  # sin tope, nunca se excede


def test_over_budget_con_budget():
    c = costs.PlanCosts("obj", budget=5)
    assert c.over_budget() is False
    c.add("a" * 40, "")  # est_in = 10 >= 5
    assert c.over_budget() is True


def test_plancosts_add_es_thread_safe():
    # El observer de track() se dispara desde varios hilos (fan-out de _h_swarm): los += van bajo
    # lock, así que 4 hilos x 1000 add no pierden cuentas (sin lock, la carrera perdería sumas).
    import threading

    c = costs.PlanCosts("obj")

    def golpea():
        for _ in range(1000):
            c.add("abcd", "abcd")  # in=1, out=1 por llamada

    hilos = [threading.Thread(target=golpea) for _ in range(4)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    assert c.dispatches == 4000
    assert c.est_in == 4000 and c.est_out == 4000


# --- track: observer + ledger --------------------------------------------------


def _fake_run(stdout="salida", returncode=0):
    def _run(*a, **k):
        return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    return _run


def test_track_instala_y_restaura_el_observer(monkeypatch):
    assert executors._observer is None
    with costs.track("obj", record=False):
        assert executors._observer is not None  # instalado dentro del bloque
    assert executors._observer is None  # restaurado al salir


def test_track_contabiliza_un_dispatch_real(monkeypatch, tmp_path):
    monkeypatch.setattr(executors.subprocess, "run", _fake_run(stdout="salida"))
    with costs.track("obj", record=False) as c:
        ok, out = executors.run_agent("hola prompt", cwd=tmp_path, mode="read")
    assert ok and out == "salida"
    assert c.dispatches == 1
    assert c.est_in == costs.estimate_tokens("hola prompt")
    assert c.est_out == costs.estimate_tokens("salida")


def test_track_registra_rollup_en_el_ledger(monkeypatch, tmp_path):
    eventos = []
    monkeypatch.setattr(costs.ledger, "record", lambda e: eventos.append(e))
    monkeypatch.setattr(executors.subprocess, "run", _fake_run())
    with costs.track("mi objetivo", budget=10) as c:
        executors.run_agent("prompt", cwd=tmp_path, mode="read")
    assert len(eventos) == 1
    ev = eventos[0]
    assert ev["action"] == "plan_cost" and ev["plan"] == "mi objetivo"
    assert ev["dispatches"] == 1 and ev["est_total_tokens"] == c.total
    assert ev["budget"] == 10


def test_track_sin_dispatches_no_escribe_ledger(monkeypatch):
    eventos = []
    monkeypatch.setattr(costs.ledger, "record", lambda e: eventos.append(e))
    with costs.track("obj"):
        pass  # nada despachado
    assert eventos == []  # no ensucia el ledger si no hubo gasto


def test_track_record_false_no_escribe_ledger(monkeypatch, tmp_path):
    eventos = []
    monkeypatch.setattr(costs.ledger, "record", lambda e: eventos.append(e))
    monkeypatch.setattr(executors.subprocess, "run", _fake_run())
    with costs.track("obj", record=False):
        executors.run_agent("prompt", cwd=tmp_path, mode="read")
    assert eventos == []


# --- Contabilidad local (mode="local"): cuentas aparte, coste 0 frente al budget ---


def test_add_local_no_cuenta_al_total_ni_al_budget():
    c = costs.PlanCosts("obj", budget=5)
    c.add_local("a" * 400, "b" * 400)  # muchos tokens locales
    assert c.local_dispatches == 1
    assert c.est_local_in > 0 and c.est_local_out > 0
    assert c.total == 0  # el local NO suma al total facturable
    assert c.over_budget() is False  # ni presiona el budget de Claude


def test_track_mode_local_va_a_add_local(monkeypatch, tmp_path):
    monkeypatch.setattr(executors.subprocess, "run", _fake_run(stdout="salida"))
    with costs.track("obj", record=False) as c:
        executors.notify_observer("local", "prompt-local", "salida-local")  # dispatch local
        executors.run_agent("prompt-cli", cwd=tmp_path, mode="read")  # dispatch online
    assert c.local_dispatches == 1 and c.dispatches == 1
    assert c.est_local_in == costs.estimate_tokens("prompt-local")
    assert c.est_local_out == costs.estimate_tokens("salida-local")
    assert c.total == costs.estimate_tokens("prompt-cli") + costs.estimate_tokens("salida")


def test_track_rollup_incluye_campos_locales(monkeypatch):
    eventos = []
    monkeypatch.setattr(costs.ledger, "record", lambda e: eventos.append(e))
    with costs.track("obj", record=True):
        executors.notify_observer("local", "p", "o")  # solo un dispatch local
    assert len(eventos) == 1  # el roll-up se escribe aunque SOLO haya habido dispatches locales
    ev = eventos[0]
    assert ev["local_dispatches"] == 1
    assert ev["est_local_in_tokens"] == costs.estimate_tokens("p")
    assert ev["est_local_out_tokens"] == costs.estimate_tokens("o")
    assert ev["dispatches"] == 0 and ev["est_total_tokens"] == 0


# --- Telemetría de fallback local->Claude (mode="local_fallback"): evento, no tokens ---


def test_add_fallback_cuenta_por_motivo_sin_tocar_budget():
    c = costs.PlanCosts("obj", budget=5)
    c.add_fallback("no_valido")
    c.add_fallback("no_valido")
    c.add_fallback("sin_respuesta")
    assert c.local_fallbacks == 3
    assert c.local_fallback_motivos == {"no_valido": 2, "sin_respuesta": 1}
    assert c.total == 0 and c.over_budget() is False  # es telemetría, no consume budget


def test_track_mode_local_fallback_va_a_add_fallback(monkeypatch):
    with costs.track("obj", record=False) as c:
        executors.notify_observer("local_fallback", "no_valido", "")  # el prompt lleva el motivo
    assert c.local_fallbacks == 1 and c.local_fallback_motivos == {"no_valido": 1}
    assert c.dispatches == 0 and c.local_dispatches == 0  # ni cli ni local: solo el fallback


def test_track_rollup_incluye_fallbacks_y_se_escribe_solo_con_fallback(monkeypatch):
    eventos = []
    monkeypatch.setattr(costs.ledger, "record", lambda e: eventos.append(e))
    with costs.track("obj", record=True):
        executors.notify_observer("local", "p", "o")  # 1 local usado
        executors.notify_observer("local_fallback", "sin_respuesta", "")  # 1 fallback
    assert len(eventos) == 1  # el roll-up se escribe aunque el gasto Claude sea 0
    ev = eventos[0]
    assert ev["local_dispatches"] == 1
    assert ev["local_fallbacks"] == 1
    assert ev["local_fallback_motivos"] == {"sin_respuesta": 1}


def test_track_solo_fallback_escribe_ledger(monkeypatch):
    # una corrida donde TODO cayó a Claude por local caído: igual se registra (para verlo en el ledger)
    eventos = []
    monkeypatch.setattr(costs.ledger, "record", lambda e: eventos.append(e))
    with costs.track("obj", record=True):
        executors.notify_observer("local_fallback", "no_disponible", "")
    assert len(eventos) == 1 and eventos[0]["local_fallbacks"] == 1
