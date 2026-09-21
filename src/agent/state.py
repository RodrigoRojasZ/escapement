"""Memoria de estado/trabajo (3er tipo de memoria): cola persistente + estado de cuota.

`~/.claude/projects/escapement/state.json`. Sobrevive interrupciones: la cola de tareas se
reanuda, y el quota-manager marca cuándo la suscripción está agotada para pausar y
retomar en la próxima ventana. Es lo que convierte a Escapement en un agente CONTINUO
(no un one-shot): escanea deuda → encola → procesa hasta el límite → reanuda.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from agent import config, fsutil

STATE = config.DATA_DIR / "state.json"

# Señales de rate-limit / cuota agotada de Claude Code (suscripción, ventana 5h/semanal).
_RATE_LIMIT = re.compile(
    r"(usage limit|rate limit|limit reached|too many requests|\bquota\b|"
    r"try again (later|in)|resets? (at|in)|5-hour limit|weekly limit)",
    re.IGNORECASE,
)


def _load() -> dict[str, Any]:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"queue": [], "throttled_until": None, "seq": 0}


def _save(state: dict[str, Any]) -> None:
    fsutil.write_text_atomic(STATE, json.dumps(state, ensure_ascii=False, indent=2))


def enqueue(tasks: list[dict[str, Any]]) -> int:
    """Encola tareas (dedup por repo+target si ya están pendientes). Devuelve cuántas se añadieron."""
    with fsutil.locked(STATE):
        state = _load()
        existing = {
            (t["repo"], t["target"]) for t in state["queue"] if t.get("status") == "pending"
        }
        added = 0
        for task in tasks:
            key = (task.get("repo"), task.get("target"))
            if key in existing:
                continue
            state["seq"] += 1
            state["queue"].append({"id": state["seq"], "status": "pending", **task})
            existing.add(key)
            added += 1
        _save(state)
        return added


def pending() -> list[dict[str, Any]]:
    return [t for t in _load()["queue"] if t.get("status") == "pending"]


def next_pending(skip: set[int] | None = None) -> dict[str, Any] | None:
    """Primera tarea pendiente cuyo id no esté en ``skip``.

    ``skip`` deja saltar tareas ya intentadas en la misma corrida (una que falló y volvió a
    ``pending`` para reintento posterior no se re-toma en el mismo barrido -> sin bucle caliente).
    """
    skip = skip or set()
    return next(
        (t for t in _load()["queue"] if t.get("status") == "pending" and t.get("id") not in skip),
        None,
    )


def mark(task_id: int, status: str, **extra: Any) -> None:
    with fsutil.locked(STATE):
        state = _load()
        for task in state["queue"]:
            if task.get("id") == task_id:
                task["status"] = status
                task["updated"] = datetime.now().isoformat(timespec="seconds")
                task.update(extra)
        _save(state)


def is_rate_limited(text: str) -> bool:
    """True si el output del dispatch parece un rate-limit de la suscripción."""
    return bool(_RATE_LIMIT.search(text or ""))


def set_throttled(until_iso: str | None) -> None:
    with fsutil.locked(STATE):
        state = _load()
        state["throttled_until"] = until_iso
        _save(state)


def throttled_until() -> str | None:
    return _load().get("throttled_until")
