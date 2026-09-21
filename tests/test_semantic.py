"""Tests del recall semántico (R3): ranking MMR opt-in en search().

Aislados: se inyecta una matriz de embeddings a mano (sin cargar el modelo fastembed) y se
monkeypatchea _embed para el vector de la query. Solo se ejercita el álgebra de search().
"""

import numpy as np

from agent.tools import semantic


def _unit(deg: float) -> np.ndarray:
    r = np.deg2rad(deg)
    return np.array([np.cos(r), np.sin(r)], dtype="float32")


# Cuatro notas en 2D: A y A' casi idénticas (racimo), B y C aparte. Query pegada al racimo A.
_META = [
    ("p", "A", "/A.md"),
    ("p", "Ap", "/Ap.md"),
    ("p", "B", "/B.md"),
    ("p", "C", "/C.md"),
]
_MATRIX = np.stack([_unit(0), _unit(6), _unit(45), _unit(90)])
_QUERY = _unit(3)  # equidistante de A (0°) y A' (6°): ambos son los más relevantes


def _patch(monkeypatch):
    monkeypatch.setattr(semantic, "_index", lambda: (_META, _MATRIX))
    monkeypatch.setattr(semantic, "_embed", lambda texts: np.stack([_QUERY]))


def _nombres(hits):
    return [h[2] for h in hits]


def test_diversity_cero_es_orden_por_relevancia(monkeypatch):
    # default 0.0 == comportamiento previo: top-k por relevancia pura (argsort de sims).
    _patch(monkeypatch)
    hits = semantic.search("q", k=2)  # diversity default 0.0
    sims = _MATRIX @ _QUERY
    esperado = [_META[i][1] for i in np.argsort(-sims)[:2]]
    assert _nombres(hits) == esperado == ["A", "Ap"]  # el racimo entero copa el top-2


def test_diversity_alta_rompe_el_racimo(monkeypatch):
    # Con diversidad, el segundo elegido ya no es el casi-duplicado (A'), sino una nota distinta.
    _patch(monkeypatch)
    hits = semantic.search("q", k=2, diversity=1.0)
    nombres = _nombres(hits)
    assert nombres[0] == "A"  # el más relevante siempre abre
    assert "Ap" not in nombres  # el redundante queda fuera
    assert nombres[1] == "C"  # el más ortogonal (90°) maximiza la marginal relevance


def test_score_devuelto_es_la_relevancia_no_el_mmr(monkeypatch):
    # El score de cada hit es la relevancia coseno a la query (para que los umbrales no cambien).
    _patch(monkeypatch)
    hits = semantic.search("q", k=2, diversity=1.0)
    sims = _MATRIX @ _QUERY
    idx_A = 0
    assert abs(hits[0][0] - float(sims[idx_A])) < 1e-6


def test_k_mayor_que_el_corpus_devuelve_todo(monkeypatch):
    _patch(monkeypatch)
    hits = semantic.search("q", k=99, diversity=0.7)
    assert len(hits) == len(_META)


def test_sin_notas_devuelve_vacio(monkeypatch):
    monkeypatch.setattr(semantic, "_index", lambda: ([], np.zeros((0, 2), dtype="float32")))
    assert semantic.search("q", k=3, diversity=0.5) == []
