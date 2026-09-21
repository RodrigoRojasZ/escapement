"""Latch persistente de la hotkey: registra las pulsaciones ocurridas FUERA de la grabación.

Sin esto, el hook de teclado solo vive mientras el micrófono está abierto (cada captura enganchaba
y desenganchaba el suyo): una F2 pulsada mientras se transcribe, mientras el modelo escribe o
mientras Escapement habla no la ve nadie y se pierde. El síntoma es un ciclo que no rota — pulsas
para hablar, no pasa nada, y hay que volver a pulsar cuando el ícono ya volvió a azul.

Aquí el hook se engancha UNA vez para toda la vida del proceso y deja la pulsación en un flag. La
captura lo consume (:meth:`wait` retorna de inmediato si ya había una) y el daemon lo consulta sin
consumirlo (:meth:`pending`) para saber que el usuario quiere el turno YA y cortar lo que esté
haciendo. Es un flag, no un contador: tres pulsaciones seguidas abren un turno, no tres.

Instancia proceso-global :data:`LATCH`, como ``controls.CONTROLS``: el hook del sistema es único.
"""

from __future__ import annotations

import threading

from agent import config


class HotkeyLatch:
    """Recuerda si la hotkey fue pulsada, con un hook de teclado de por vida.

    Args:
        key: tecla a vigilar; ``None`` (default) resuelve ``config.VOICE_HOTKEY`` en cada uso
            (así un cambio de config no queda congelado en el constructor).
    """

    def __init__(self, key: str | None = None) -> None:
        self._key = key
        self._pulsada = threading.Event()
        self._lock = threading.Lock()
        self._hook: object | None = None

    @property
    def key(self) -> str:
        """La tecla vigilada (``config.VOICE_HOTKEY`` si no se fijó una en el constructor)."""
        return self._key or config.VOICE_HOTKEY

    def start(self) -> None:
        """Engancha el hook de teclado si aún no lo está. Idempotente.

        Propaga la excepción si ``keyboard`` no está disponible: sin hook no hay captura por
        hotkey, y silenciarlo dejaría al daemon esperando un turno que nunca llega.
        """
        with self._lock:
            if self._hook is not None:
                return
            import keyboard

            self._hook = keyboard.on_press_key(self.key, lambda _e: self._pulsada.set())

    def stop(self) -> None:
        """Desengancha el hook y descarta lo pendiente. Idempotente."""
        with self._lock:
            hook, self._hook = self._hook, None
        if hook is not None:
            try:
                import keyboard

                keyboard.unhook(hook)
            except Exception:
                pass  # el proceso se está cerrando; un hook huérfano muere con él
        self._pulsada.clear()

    def press(self) -> None:
        """Marca una pulsación a mano (lo usa el hook; los tests simulan la tecla con esto)."""
        self._pulsada.set()

    def pending(self) -> bool:
        """True si hay una pulsación sin consumir. NO la consume.

        Es la consulta del daemon: "¿el usuario pidió el turno mientras yo trabajaba?". Quien
        abre el micrófono es el único que consume, con :meth:`take` o :meth:`wait`.
        """
        return self._pulsada.is_set()

    def take(self) -> bool:
        """Consume la pulsación pendiente; True si la había."""
        if self._pulsada.is_set():
            self._pulsada.clear()
            return True
        return False

    def clear(self) -> None:
        """Descarta lo pendiente sin abrir turno.

        La usa la captura en sus dos bordes: la pulsación que ACTIVA la grabación y la que la
        CIERRA (segundo toque) ya cumplieron su función; si quedaran en el flag reabrirían el
        micrófono solo, en bucle.
        """
        self._pulsada.clear()

    def wait(self, timeout: float | None = None) -> bool:
        """Espera una pulsación y la consume.

        Args:
            timeout: segundos máximos de espera; ``None`` = sin límite.

        Returns:
            True si había una pendiente (retorno inmediato) o llegó dentro de ``timeout``;
            False si expiró. El retorno inmediato es el punto del módulo: la F2 pulsada durante
            la transcripción abre el turno siguiente sin que el usuario tenga que repetirla.
        """
        if self.take():
            return True
        self.start()
        if self._pulsada.wait(timeout):
            self._pulsada.clear()
            return True
        return False


LATCH = HotkeyLatch()  # hook único del proceso, compartido por la captura y el daemon
