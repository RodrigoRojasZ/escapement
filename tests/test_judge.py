"""Tests del verifier adversarial (parseo + fail-open + voto por mayoría). Sin cuota."""

from agent.judge import Judgment, _parse, _vote, judge_diff


def _J(v, issues=None):
    return Judgment(v, issues or [])


def test_parse_seguro():
    j = _parse('{"veredicto": "SEGURO", "problemas": []}')
    assert j.verdict == "SEGURO" and j.ok and j.issues == []


def test_parse_riesgoso_con_texto_alrededor():
    j = _parse('Aquí va: {"veredicto":"RIESGOSO","problemas":["cambia el orden de salida"]} listo')
    assert j.verdict == "RIESGOSO" and not j.ok
    assert j.issues == ["cambia el orden de salida"]


def test_parse_basura_es_desconocido_y_no_bloquea():
    j = _parse("no hay json aquí")
    assert j.verdict == "DESCONOCIDO" and j.ok  # fail-open: no bloquea el PR


def test_parse_veredicto_raro_es_desconocido():
    j = _parse('{"veredicto": "quizás", "problemas": []}')
    assert j.verdict == "DESCONOCIDO"


def test_judge_diff_vacio_no_invoca_claude():
    # Sin diff no hay nada que juzgar: SEGURO inmediato, sin subprocess.
    assert judge_diff("", "optimiza x").verdict == "SEGURO"


# --- Voto por mayoría (Fase 0: N-jueces) ---


def test_vote_unanime_seguro():
    assert _vote([_J("SEGURO"), _J("SEGURO"), _J("SEGURO")]).verdict == "SEGURO"


def test_vote_mayoria_segura_gana():
    v = _vote([_J("SEGURO"), _J("SEGURO"), _J("RIESGOSO", ["x"])])
    assert v.verdict == "SEGURO" and v.ok


def test_vote_mayoria_riesgosa_bloquea_y_une_problemas():
    v = _vote([_J("RIESGOSO", ["a", "b"]), _J("RIESGOSO", ["b", "c"]), _J("SEGURO")])
    assert v.verdict == "RIESGOSO" and not v.ok
    assert v.issues == ["a", "b", "c"]  # dedup con orden estable


def test_vote_empate_es_conservador():
    # 1 seguro vs 1 riesgoso (el tercero se abstiene): sin mayoría estricta de seguros -> bloquea.
    v = _vote([_J("SEGURO"), _J("RIESGOSO", ["z"]), _J("DESCONOCIDO")])
    assert v.verdict == "RIESGOSO"


def test_vote_todos_desconocidos_no_bloquea():
    v = _vote([_J("DESCONOCIDO"), _J("DESCONOCIDO")])
    assert v.verdict == "DESCONOCIDO" and v.ok  # fail-open


def test_judge_diff_agrega_los_votos(monkeypatch):
    from agent import executors, judge

    salidas = iter(
        [
            '{"veredicto":"SEGURO","problemas":[]}',
            '{"veredicto":"RIESGOSO","problemas":["cambia el orden"]}',
            '{"veredicto":"SEGURO","problemas":[]}',
        ]
    )
    monkeypatch.setattr(executors, "run_agent", lambda *a, **k: (True, next(salidas)))
    j = judge.judge_diff("diff no vacío", "optimiza", voters=3)
    assert j.verdict == "SEGURO"  # 2 seguros > 1 riesgoso


def test_judge_diff_un_solo_juez_preserva_desconocido(monkeypatch):
    from agent import executors, judge

    monkeypatch.setattr(executors, "run_agent", lambda *a, **k: (True, "sin json"))
    j = judge.judge_diff("diff no vacío", "optimiza", voters=1)
    assert j.verdict == "DESCONOCIDO" and j.ok  # 1 juez mudo no bloquea (comportamiento previo)
