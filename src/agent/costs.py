"""Contabilidad de costo por plan (R3): estima tokens de cada dispatch y aplica un budget opcional.

NO hay conteo real de tokens: el executor headless (``claude -p`` en modo texto) no emite ``usage``
sin cambiar a ``--output-format json``, lo que rompería el parseo de texto de todos los callers
(planner/runner/juez). Se usa una ESTIMACIoN transparente (~4 chars/token) como proxy para
presupuestar y observar el gasto, NUNCA como factura. :func:`track` instala un observer en el sink
uníco (``executors.run_agent``), acumula lo despachado durante una corrida, registra un roll-up en
el ledger y —si hay budget— deja que el runner corte cuando la estimación lo supera.

Opt-in y behavior-preserving: sin budget el tope nunca se cumple; el observer se instala y se quita
en el ``with``, y su fallo jamás tumba un dispatch (ver ``executors.set_dispatch_observer``).
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field

from agent import executors, ledger


def estimate_tokens(text: str) -> int:
    """Estimacion rustica de tokens de ``text`` (~4 chars/token). Proxy para budget, no factura."""
    return (len(text or "") + 3) // 4


@dataclass
class PlanCosts:
    """Acumulador del gasto ESTIMADO de una corrida del runner sobre un plan.

    Args:
        plan: objetivo del plan (etiqueta del roll-up en el ledger).
        budget: tope de tokens estimados; None (default) = sin tope, :meth:`over_budget` siempre False.
    """

    plan: str
    budget: int | None = None
    dispatches: int = 0
    est_in: int = 0
    est_out: int = 0
    local_dispatches: int = 0
    est_local_in: int = 0
    est_local_out: int = 0
    local_fallbacks: int = 0
    local_fallback_motivos: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add(self, prompt: str, output: str) -> None:
        """Contabiliza un dispatch a Claude/CLI: suma tokens estimados del prompt (in) y la salida (out).

        Thread-safe: el observer de :func:`track` puede dispararse desde varios hilos a la vez
        (p.ej. el fan-out de ``_h_swarm``), así que los ``+=`` van bajo lock para no perder cuentas.
        """
        with self._lock:
            self.dispatches += 1
            self.est_in += estimate_tokens(prompt)
            self.est_out += estimate_tokens(output)

    def add_local(self, prompt: str, output: str) -> None:
        """Contabiliza un dispatch al LLM LOCAL, en cuentas aparte (coste 0 frente al budget de Claude).

        Los tokens locales NO alimentan :meth:`total` ni :meth:`over_budget` —el ahorro del local es
        precisamente no consumir cuota online—; se registran solo para observabilidad en el ledger.
        Mismo lock que :meth:`add`: el swarm despacha desde varios hilos.
        """
        with self._lock:
            self.local_dispatches += 1
            self.est_local_in += estimate_tokens(prompt)
            self.est_local_out += estimate_tokens(output)

    def add_fallback(self, motivo: str) -> None:
        """Registra un paso que intentó el LLM local y CAYÓ a Claude, con su ``motivo``.

        Complementa :meth:`add_local` (dispatches locales USADOS): juntos dan la tasa de fallback
        local→Claude (``local_fallbacks / (local_dispatches + local_fallbacks)``), la señal para
        calibrar el ruteo. No toca budget ni tokens —solo cuenta el evento—. ``motivo`` ∈
        ``{"no_disponible", "sin_respuesta", "no_valido"}``. Mismo lock: el swarm es multi-hilo.
        """
        with self._lock:
            self.local_fallbacks += 1
            self.local_fallback_motivos[motivo] = self.local_fallback_motivos.get(motivo, 0) + 1

    @property
    def total(self) -> int:
        """Tokens estimados totales facturables (Claude/CLI): in + out. El local NO cuenta aquí."""
        return self.est_in + self.est_out

    def over_budget(self) -> bool:
        """True si hay budget y el gasto estimado ya lo alcanzó/superó (sin budget: siempre False)."""
        return self.budget is not None and self.total >= self.budget


@contextlib.contextmanager
def track(plan: str, *, budget: int | None = None, record: bool = True) -> Iterator[PlanCosts]:
    """Instrumenta todos los dispatches del bloque y devuelve el acumulador.

    Instala un observer en ``executors`` durante el ``with`` (restaura el previo al salir) para
    contabilizar cada dispatch sin tocar la firma de ``run_agent`` ni de los handlers. Al cerrar,
    si hubo dispatches y ``record`` es True, escribe un evento ``plan_cost`` en el ledger.

    Args:
        plan: objetivo del plan (etiqueta del roll-up).
        budget: tope opcional de tokens estimados (ver :class:`PlanCosts`).
        record: si True (default), registra el roll-up en el ledger al terminar; False en tests.
    """
    costos = PlanCosts(plan, budget=budget)

    def _observe(mode: str, prompt: str, output: str) -> None:
        # El backend local (mode="local") va a cuentas aparte: no consume budget de Claude.
        # "local_fallback" es un evento de telemetría (paso local que cayó a Claude): el 'prompt'
        # transporta el motivo y no suma tokens (el dispatch a Claude se cuenta aparte vía run_agent).
        if mode == "local":
            costos.add_local(prompt, output)
        elif mode == "local_fallback":
            costos.add_fallback(prompt)
        else:
            costos.add(prompt, output)

    prev = executors.set_dispatch_observer(_observe)
    try:
        yield costos
    finally:
        executors.set_dispatch_observer(prev)
        if record and (costos.dispatches or costos.local_dispatches or costos.local_fallbacks):
            ledger.record(
                {
                    "action": "plan_cost",
                    "plan": (plan or "")[:200],
                    "dispatches": costos.dispatches,
                    "est_in_tokens": costos.est_in,
                    "est_out_tokens": costos.est_out,
                    "est_total_tokens": costos.total,
                    "local_dispatches": costos.local_dispatches,
                    "est_local_in_tokens": costos.est_local_in,
                    "est_local_out_tokens": costos.est_local_out,
                    "local_fallbacks": costos.local_fallbacks,
                    "local_fallback_motivos": costos.local_fallback_motivos,
                    "budget": budget,
                    "over_budget": costos.over_budget(),
                }
            )
