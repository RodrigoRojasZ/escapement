"""Bus de eventos in-process con journal durable (``data/events.jsonl``).

Fase 0 del desacoplamiento presencia<->cerebro: los productores (daemon de voz, runner, convlog)
publican eventos por topic y los consumidores in-process se suscriben por prefijo. Cada evento
con ``journal=True`` queda además en ``config.EVENTS`` (JSONL append-only, mismo patrón que el
ledger), de modo que un proceso externo pueda seguir el flujo leyendo el archivo, sin servidor
HTTP. Cuando exista un frontend (p.ej. AIRI), su adaptador se monta SOBRE estos contratos.

Topics actuales (contratos):

- ``turn``         ``{backend, session_id, user, reply}`` — cada turno usuario<->agente.
                   Se publica con ``journal=False``: el texto ya es durable en
                   ``conversations.jsonl`` (convlog); aquí es solo señal in-process.
- ``voice.state``  ``{state: "activo" | "congelado" | "apagado"}`` — ciclo del daemon de voz.
- ``voice.barge_in`` ``{}`` — F2 cortó al TTS mientras hablaba (interrupción del usuario).
- ``voice.wake``   ``{model}`` — la wake word abrió un turno manos libres (sin F2).
- ``voice.capture`` ``{state: "escuchando" | "transcribiendo" | "pensando" | "hablando" |
                   "inactivo"}`` — fase del ciclo de voz, para la presencia (el ícono de bandeja
                   cambia de color). El ciclo entero está cubierto, así que ``inactivo`` significa
                   de verdad "no estoy haciendo nada". Solo se publica al CAMBIAR de fase (ver
                   ``ears.publicar_captura``) y con ``journal=False``: es señal de UI, no un hecho.
- ``plan.step_start`` ``{repo, id, tipo, accion, hechos, total}`` — el runner ARRANCA un paso.
                   Progreso en vivo (un dispatch tarda minutos y ``plan.step`` recién llega al
                   terminarlo), con ``journal=False``: el hecho durable es ``plan.step``.
- ``plan.step``    ``{repo, id, tipo, estado, nota}`` — cada paso ejecutado por el runner.
- ``plan.eval``    ``{repo, veredicto, gaps}`` — veredicto de la evaluación global al agotar el
                   roadmap (E2, deuda #7). ``veredicto`` es ``"done"``, ``"incompleto"`` o ``""``
                   si la evaluación no produjo salida usable (best-effort); ``gaps`` (lista de
                   strings) solo viene poblada con ``incompleto``.
- ``plan.replan``  ``{repo, anadidos, ciclo, max}`` — la replanificación automática convirtió los
                   gaps en ``anadidos`` pasos nuevos; ``ciclo`` es el contador persistido tras
                   incrementarlo y ``max`` el tope vigente (``AGENT_PLAN_REPLAN_MAX``).
- ``plan.done``    ``{repo, objetivo, completado}`` — fin de una corrida de ``run_plan``.

Silencioso y best-effort: publicar nunca lanza (un consumidor roto o un disco lleno no deben
tumbar al productor). Importar este módulo no toca red ni escribe disco.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from agent import config

Callback = Callable[["Event"], None]

_LOCK = threading.Lock()
_SUBS: list[tuple[str, Callback]] = []


@dataclass(frozen=True)
class Event:
    """Un evento del bus: topic + payload + origen + timestamp."""

    topic: str
    data: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    ts: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Representación JSON-serializable (la misma línea que va al journal)."""
        return asdict(self)


def subscribe(prefix: str, callback: Callback) -> Callable[[], None]:
    """Registra un consumidor in-process para los topics que empiecen con ``prefix``.

    Args:
        prefix: prefijo de topic a escuchar. ``"plan."`` recibe ``plan.step`` y ``plan.done``;
            ``""`` (cadena vacía) recibe todos los eventos. El match es ``startswith``, sin
            comodines.
        callback: invocado con cada :class:`Event` que matchee, en el hilo del productor.
            Debe ser rápido y no bloquear; sus excepciones se tragan (no afectan al productor
            ni al resto de consumidores).

    Returns:
        Función sin argumentos que cancela esta suscripción (idempotente).
    """
    entry = (prefix, callback)
    with _LOCK:
        _SUBS.append(entry)

    def unsubscribe() -> None:
        with _LOCK:
            try:
                _SUBS.remove(entry)
            except ValueError:
                pass

    return unsubscribe


def publish(
    topic: str,
    data: dict[str, Any] | None = None,
    *,
    source: str = "",
    journal: bool = True,
) -> Event:
    """Publica un evento: lo persiste en el journal y lo entrega a los suscriptores.

    Args:
        topic: nombre jerárquico con puntos (``"plan.step"``, ``"voice.state"``).
        data: payload JSON-serializable del evento; ``None`` equivale a ``{}``.
        source: quién lo emite (``"voz"``, ``"runner"``, ...); ``""`` si no aplica.
        journal: si True (default), añade la línea a ``config.EVENTS``. Usa False cuando el
            contenido ya es durable en otro archivo (p.ej. los turnos viven en
            ``conversations.jsonl``) y solo interesa la señal in-process.

    Returns:
        El :class:`Event` publicado (con timestamp asignado). Best-effort: nunca lanza.
    """
    event = Event(
        topic=topic,
        data=dict(data or {}),
        source=source,
        ts=datetime.now().isoformat(timespec="seconds"),
    )
    if journal:
        try:
            config.EVENTS.parent.mkdir(parents=True, exist_ok=True)
            with config.EVENTS.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.to_dict(), ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass  # el journal es observabilidad, no puede tumbar al productor
    with _LOCK:
        subs = list(_SUBS)
    for prefix, callback in subs:
        if event.topic.startswith(prefix):
            try:
                callback(event)
            except Exception:
                pass  # un consumidor roto no afecta al productor ni al resto
    return event


def read(limit: int | None = None, *, topic: str | None = None) -> list[dict[str, Any]]:
    """Lee eventos del journal (``config.EVENTS``).

    Args:
        limit: si se indica, devuelve solo los últimos ``limit`` (tras filtrar); ``None`` = todos.
        topic: prefijo de topic para filtrar (mismo match que :func:`subscribe`); ``None`` = sin
            filtro.

    Returns:
        Lista de dicts con la forma de :meth:`Event.to_dict`, en orden cronológico.
    """
    if not config.EVENTS.exists():
        return []
    events = [
        json.loads(line)
        for line in config.EVENTS.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip()
    ]
    if topic is not None:
        events = [e for e in events if str(e.get("topic", "")).startswith(topic)]
    return events[-limit:] if limit else events


def _reset() -> None:
    """Quita todos los suscriptores (solo para tests)."""
    with _LOCK:
        _SUBS.clear()
