"""Registro de conversaciones (append-only JSONL): cada turno usuario -> respuesta.

Cierra el hueco de observabilidad del diálogo: el ``ledger`` cubre el orquestador (optimize/
review), no el chat. Aquí queda TODO turno —los locales (qwen) y los de Claude—, escrito desde
el único punto por donde pasan (REPL de texto y daemon de voz), relegible con ``escapement
historial``. Silencioso: nunca imprime (respeta la regla de no meter más logs a stdout). Vive
junto al ledger en ``config.DATA_DIR`` (dentro del repo, gitignorado), separado del vault de memoria.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from agent import bus, config


def record(
    user: str,
    reply: str,
    *,
    backend: str,
    session_id: str | None = None,
    duration_s: float | None = None,
) -> None:
    """Añade un turno al registro (append-only, una línea JSON con timestamp).

    Args:
        user: lo que dijo el usuario (ya transcrito, en modo voz).
        reply: lo que respondió el agente.
        backend: motor que respondió: ``"local"`` (qwen) o ``"claude"``.
        session_id: id del hilo de Claude cuando aplique (correlaciona con el resume); ``None``
            en turnos locales, que no tienen sesión del SDK.
        duration_s: latencia del turno en segundos, si se midió; ``None`` si no.
    """
    config.CONVERSATIONS.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "backend": backend,
        "session_id": session_id,
        "user": user,
        "reply": reply,
        "duration_s": duration_s,
    }
    with config.CONVERSATIONS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    # Señal in-process para consumidores del bus (journal=False: el turno ya es durable aquí).
    bus.publish(
        "turn",
        {"backend": backend, "session_id": session_id, "user": user, "reply": reply},
        source=backend,
        journal=False,
    )


def read(limit: int | None = None) -> list[dict[str, Any]]:
    """Lee los turnos registrados (los últimos ``limit`` si se indica)."""
    if not config.CONVERSATIONS.exists():
        return []
    events = [
        json.loads(line)
        for line in config.CONVERSATIONS.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip()
    ]
    return events[-limit:] if limit else events
