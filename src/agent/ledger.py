"""Memoria operacional (episódica) de la auto-evolución.

Registro append-only (JSONL) de cada tarea del orquestador: directiva, repo, rama, PR,
tests, y —tras tu review— si el PR se aceptó o rechazó. Es lo que permite a Escapement
NO repetir trabajo y aprender de tus decisiones de merge. Vive en ``config.DATA_DIR``
(dentro del repo, gitignorado), separado de la memoria de conocimiento.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from agent import config


def record(event: dict[str, Any]) -> None:
    """Añade un evento al ledger (append-only, una línea JSON con timestamp).

    Si el archivo terminó en una línea a medias (append interrumpido, sin ``\\n`` final),
    antepone un salto: así el evento nuevo no se concatena a la línea torn (que quedaría
    malformada a mitad del archivo y arrastraría también este evento válido).
    """
    config.LEDGER.parent.mkdir(parents=True, exist_ok=True)
    stamped = {"ts": datetime.now().isoformat(timespec="seconds"), **event}
    line = json.dumps(stamped, ensure_ascii=False, default=str) + "\n"
    with config.LEDGER.open("ab") as fh:
        if fh.tell() > 0:
            with config.LEDGER.open("rb") as rb:
                rb.seek(-1, 2)
                if rb.read(1) != b"\n":
                    fh.write(b"\n")  # cierra la línea torn: el evento nuevo no se contamina
        fh.write(line.encode("utf-8"))


def read(limit: int | None = None) -> list[dict[str, Any]]:
    """Lee los eventos del ledger (los últimos ``limit`` si se indica).

    Tolerante a líneas malformadas: un append interrumpido a mitad deja una línea JSON
    truncada — se salta esa línea en vez de invalidar el ledger completo.
    """
    if not config.LEDGER.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in config.LEDGER.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue  # línea truncada/corrupta: se ignora, el resto del ledger sigue vivo
    return events[-limit:] if limit else events


def last_optimize(repo: str, target: str) -> dict[str, Any] | None:
    """Último evento de optimize sobre (repo, target), o None — para no repetir tareas."""
    for event in reversed(read()):
        if (
            event.get("action") == "optimize"
            and event.get("target") == target
            and event.get("repo") == str(repo)
        ):
            return event
    return None


def latest_pr_reviews() -> dict[str, str]:
    """Último resultado de review por PR: ``{pr_url: 'accepted'|'rejected'|'pending'}``.

    Un PR puede revisarse varias veces (pending -> accepted); gana el evento más reciente.
    """
    out: dict[str, str] = {}
    for event in read():
        if event.get("action") == "review" and event.get("pr_url"):
            out[event["pr_url"]] = event.get("result", "pending")
    return out


def rejected_targets(repo: str) -> set[str]:
    """Targets cuyo PR rechazaste en ``repo`` — para no volver a proponerlos (aprendizaje)."""
    reviews = latest_pr_reviews()
    return {
        event["target"]
        for event in read()
        if event.get("action") == "optimize"
        and event.get("repo") == str(repo)
        and reviews.get(event.get("pr_url", "")) == "rejected"
    }


def stats(action: str = "optimize", limit: int | None = None) -> dict[str, Any]:
    """Métricas agregadas de observabilidad (Fase 0) sobre los eventos de ``action``.

    total, verificadas, tasa de éxito, bloqueadas por secretos, duración promedio y la
    distribución por executor — para auditar qué hace el agente sin leer el JSONL a mano.
    """
    from collections import Counter

    rows = [e for e in read(limit) if e.get("action") == action]
    total = len(rows)
    ok = sum(1 for e in rows if e.get("verified"))
    durations = [e["duration_s"] for e in rows if isinstance(e.get("duration_s"), int | float)]
    return {
        "total": total,
        "verificadas": ok,
        "tasa_exito": round(ok / total, 2) if total else 0.0,
        "bloqueadas_secretos": sum(1 for e in rows if e.get("blocked_secrets")),
        "duracion_prom_s": round(sum(durations) / len(durations), 1) if durations else 0.0,
        "por_executor": dict(Counter(e.get("executor", "?") for e in rows)),
    }
