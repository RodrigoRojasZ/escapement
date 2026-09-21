"""Recall semántico sobre el vault (idea extraída de khoj, implementación propia ligera).

Embeddings ONNX (fastembed, sin torch, reusa onnxruntime) + similitud coseno en memoria
(numpy). Para ~56 notas no hace falta Postgres/Chroma. Complementa el recall keyword;
ambos read-only (Anillo 0). 100% local, sin egress.

El grupo de deps es opcional: `uv sync --group semantic`.
"""

from __future__ import annotations

import glob
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from agent import config

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"  # dim 384, es/en
_DIM = 384


@lru_cache(maxsize=1)
def _embedder():
    import warnings

    from fastembed import TextEmbedding

    # fastembed >=0.6 cambió el pooling de este modelo (CLS -> mean). El mean pooling es el correcto
    # para sentence-transformers como MiniLM (así se entrenó) y NO nos afecta: el índice es efímero
    # (re-embebemos query+corpus cada sesión, sin vectores persistidos), así que el coseno sigue
    # coherente. Silenciamos SOLO ese aviso para no ensuciar el output; no pineamos la versión vieja.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*mean pooling instead of CLS.*")
        return TextEmbedding(model_name=MODEL)


def _iter_notes() -> list[tuple[str, str, str, str]]:
    """[(proyecto, nombre, ruta, texto)] de todas las notas del vault (excluye MEMORY.md)."""
    notes: list[tuple[str, str, str, str]] = []
    for mem in glob.glob(str(config.PROJECTS_DIR / "*" / "memory")):
        project = Path(mem).parent.name
        for md in glob.glob(os.path.join(mem, "*.md")):
            name = Path(md).stem
            if name == "MEMORY":
                continue
            try:
                text = Path(md).read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            notes.append((project, name, md, text))
    return notes


def _embed(texts: list[str]):
    import numpy as np

    arr = np.array(list(_embedder().embed(texts)), dtype="float32")
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return arr / norms  # normalizado -> coseno = producto punto


@lru_cache(maxsize=1)
def _index():
    """Indexa el vault una vez por sesión. Devuelve (meta, matriz_normalizada)."""
    import numpy as np

    notes = _iter_notes()
    if not notes:
        return [], np.zeros((0, _DIM), dtype="float32")
    docs = [f"{name}\n{text[:2000]}" for (_p, name, _path, text) in notes]
    matrix = _embed(docs)
    meta = [(p, name, path) for (p, name, path, _t) in notes]
    return meta, matrix


def search(query: str, k: int = 5, diversity: float = 0.0) -> list[tuple[float, str, str, str]]:
    """Top-k notas por similitud semántica: [(score, proyecto, nombre, ruta)].

    Args:
        k: cuántas notas devolver (se fuerza a >=1).
        diversity: mezcla MMR (Maximal Marginal Relevance) en [0, 1] para penalizar la redundancia
            ENTRE los resultados. ``0.0`` (default) = orden puro por relevancia, IDÉNTICO al
            comportamiento previo. Hacia ``1.0`` privilegia que las notas elegidas sean distintas
            entre sí (menos solapadas), útil cuando el top-k trae varias casi-iguales. El ``score``
            devuelto siempre es la relevancia coseno a la query (no el score MMR interno), para que
            cualquier umbral del caller siga significando lo mismo.
    """
    import numpy as np

    meta, matrix = _index()
    if not meta:
        return []
    k = max(1, k)
    q = _embed([query])[0]
    sims = matrix @ q
    lam = 1.0 - min(1.0, max(0.0, diversity))  # peso de la relevancia en la mezcla MMR
    # Sin diversidad (o pidiendo todo) no hay nada que reordenar: top-k por relevancia (ruta previa).
    if lam >= 1.0 or k >= len(meta):
        order = np.argsort(-sims)[:k]
        return [(float(sims[i]), *meta[i]) for i in order]
    # MMR: sobre un pool de los más relevantes, elige iterativamente el candidato que maximiza
    # lam*relevancia - (1-lam)*máxima_similitud_con_lo_ya_elegido (la matriz está normalizada, así
    # que matrix[i] @ matrix[j] es el coseno). El primero es siempre el más relevante.
    pool = list(np.argsort(-sims)[: max(k * 5, 25)])
    elegidos: list[int] = [pool.pop(0)]
    while pool and len(elegidos) < k:
        ya = matrix[elegidos]  # (m, dim) normalizada
        mejor_i, mejor_score = pool[0], None
        for i in pool:
            redundancia = float(np.max(ya @ matrix[i]))
            score = lam * float(sims[i]) - (1.0 - lam) * redundancia
            if mejor_score is None or score > mejor_score:
                mejor_i, mejor_score = i, score
        elegidos.append(mejor_i)
        pool.remove(mejor_i)
    return [(float(sims[i]), *meta[i]) for i in elegidos]


@tool(
    "recall_semantic",
    "Busca en la memoria por SIGNIFICADO (embeddings), no palabras exactas. Úsalo cuando el "
    "recall por keyword no encuentre nada, o para buscar por intención/concepto. Read-only. "
    "'diversity' (0..1, opcional) diversifica los resultados si el top-k trae notas casi-iguales.",
    {"query": str, "k": int, "diversity": float},
)
async def recall_semantic(args: dict[str, Any]) -> dict[str, Any]:
    try:
        hits = search(
            args.get("query", ""),
            int(args.get("k") or 5),
            diversity=float(args.get("diversity") or 0.0),
        )
    except ImportError:
        return {
            "content": [
                {
                    "type": "text",
                    "text": "recall_semantic no disponible (instala: uv sync --group semantic)",
                }
            ],
            "is_error": True,
        }
    if not hits:
        return {"content": [{"type": "text", "text": "(sin notas indexadas)"}]}
    lines = []
    for score, project, name, path in hits:
        try:
            snippet = " ".join(Path(path).read_text(encoding="utf-8", errors="replace").split())[
                :200
            ]
        except Exception:
            snippet = ""
        lines.append(f"[{score:.2f}] {project}/{name}: {snippet}")
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


semantic_server = create_sdk_mcp_server("semantic", version="0.1.0", tools=[recall_semantic])
