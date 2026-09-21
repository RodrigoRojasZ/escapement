"""Señal de cancelación cooperativa para el trabajo largo que corre en un hilo.

Las acciones caras del orquestador (``run_plan`` sobre todo) se despachan con
``asyncio.to_thread`` para no bloquear el event loop del agente. Un hilo de Python no se puede
matar desde afuera, así que la única cancelación honesta es **cooperativa**: quien pide parar
levanta una bandera y el trabajo la consulta en sus puntos de corte naturales (entre pasos, nunca
a mitad de un despacho).

Vive en su propio módulo, sin dependencias, justamente para que lo pueda importar tanto el
productor de la señal (el daemon de voz, cuando F2 corta el turno) como el consumidor (el runner,
que es pesado de importar) sin acoplarlos entre sí.

:data:`RUN` es la señal de la corrida del plan; hay una sola porque ``_RUN_LOCK`` ya garantiza una
corrida a la vez en el proceso.
"""

from __future__ import annotations

import threading

__all__ = ["CancelSignal", "RUN"]


class CancelSignal:
    """Bandera de "para cuando puedas", con el motivo de quien la levantó.

    Segura entre hilos: la levanta el hilo del que pide (el loop de voz) y la consulta el hilo
    que trabaja (el runner).
    """

    __slots__ = ("_evento", "_lock", "_motivo")

    def __init__(self) -> None:
        self._evento = threading.Event()
        self._lock = threading.Lock()
        self._motivo = ""

    def pedir(self, motivo: str = "") -> None:
        """Pide la cancelación del trabajo en curso.

        Args:
            motivo: por qué se cancela, en lenguaje humano. Queda disponible en :meth:`motivo`
                para que el trabajo lo deje anotado (p.ej. la nota del paso que quedó a medias).
                ``""`` = sin explicación.
        """
        with self._lock:
            self._motivo = motivo
        self._evento.set()

    def limpiar(self) -> None:
        """Baja la bandera y olvida el motivo. Lo llama quien ARRANCA un trabajo nuevo."""
        with self._lock:
            self._motivo = ""
        self._evento.clear()

    def pedido(self) -> bool:
        """True si alguien pidió cancelar y nadie limpió la señal desde entonces."""
        return self._evento.is_set()

    def motivo(self) -> str:
        """Motivo de la última cancelación pedida (``""`` si no hay o no se dio ninguno)."""
        with self._lock:
            return self._motivo


# Señal de la corrida del plan (`runner.run_plan`). Global porque la corrida también lo es:
# `orchestrator._RUN_LOCK` deja una sola por proceso.
RUN = CancelSignal()
