"""Tests de los evals (métricas de calidad). Sin cuota."""

from agent.evals import evaluate, metrics


def test_metrics_cobertura(tmp_path):
    (tmp_path / "m.py").write_text(
        "def f(a: int) -> int:\n    '''doc'''\n    return a\n\n\ndef g(a):\n    return a\n",
        encoding="utf-8",
    )
    m = metrics(tmp_path / "m.py")
    assert m["funcs"] == 2 and m["hint_cov"] == 0.5 and m["doc_cov"] == 0.5


def test_evaluate_detecta_mejora():
    ev = evaluate({"hint_cov": 0.0, "doc_cov": 0.0}, {"hint_cov": 1.0, "doc_cov": 1.0})
    assert ev.improved


def test_evaluate_sin_cambio_no_es_mejora():
    ev = evaluate({"hint_cov": 1.0, "doc_cov": 1.0}, {"hint_cov": 1.0, "doc_cov": 1.0})
    assert not ev.improved


def test_evaluate_regresion_no_es_mejora():
    ev = evaluate({"hint_cov": 1.0, "doc_cov": 1.0}, {"hint_cov": 0.5, "doc_cov": 1.0})
    assert not ev.improved
