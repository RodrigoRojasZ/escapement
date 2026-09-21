"""Canal de control entre el tray (hilo *detached*) y el daemon de voz (hilo principal).

El menú del tray corre en su propio hilo; para acciones que afectan al loop del daemon —pausar
la escucha, empezar un tema nuevo— necesita señalizarlo de forma thread-safe. El daemon consulta
estas banderas en su loop, así que la acción se materializa en el siguiente ciclo (tras el próximo
F2 o el timeout de congelamiento). Es una instancia proceso-global (``CONTROLS``) compartida por
ambos hilos; ``threading.Event`` da la sincronización sin locks explícitos.
"""

from __future__ import annotations

import threading


class Controls:
    """Banderas compartidas tray -> daemon (pausa de escucha y reinicio de hilo)."""

    def __init__(self) -> None:
        self._paused = threading.Event()  # escucha en pausa: el daemon ignora los turnos
        self._reset = threading.Event()  # "nuevo tema" pedido desde el tray

    # --- Pausa de escucha ---
    def toggle_pause(self) -> bool:
        """Alterna la pausa. Devuelve el nuevo estado (True = pausado)."""
        if self._paused.is_set():
            self._paused.clear()
        else:
            self._paused.set()
        return self._paused.is_set()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    # --- Nuevo tema (reset del hilo) ---
    def request_reset(self) -> None:
        """Marca que el daemon debe reiniciar el hilo en su próximo ciclo."""
        self._reset.set()

    def take_reset(self) -> bool:
        """True (consumiendo la señal) si había un reinicio de hilo pendiente."""
        if self._reset.is_set():
            self._reset.clear()
            return True
        return False


CONTROLS = Controls()  # instancia compartida entre el tray y el daemon
