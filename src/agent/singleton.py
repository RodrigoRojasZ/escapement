"""Lock de instancia única, portable y sin dependencias: bind a un puerto de loopback.

El SO libera el socket al morir el proceso, así que nunca quedan locks huérfanos (a diferencia
de un lockfile con PID). Si el ``bind`` falla, ya hay otra instancia viva tomando el puerto. Lo
usa el daemon de voz residente para no duplicarse: un doble clic en el launcher levantaría un
segundo tray + hotkey + modelos; con el lock, la segunda instancia detecta la primera y sale.
"""

from __future__ import annotations

import socket

# Puerto de loopback que actúa de mutex entre instancias (no se escucha tráfico; solo se ocupa).
_LOCK_PORT = 49731
_held: list[socket.socket] = []  # refs vivas: el lock dura lo que viva el proceso (no GC)


def acquire_single_instance(port: int = _LOCK_PORT) -> bool:
    """Intenta tomar el lock de instancia única. True si lo tomó ESTE proceso; False si ya existía.

    Args:
        port: puerto de loopback usado como mutex; debe ser el mismo en todas las instancias.
            Sin ``SO_REUSEADDR`` el segundo ``bind`` al puerto ocupado falla (Windows y Linux),
            que es justo la señal de "ya hay otra instancia".
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        sock.close()
        return False
    _held.append(sock)  # mantener la referencia viva mantiene el lock tomado
    return True


def release_single_instance() -> None:
    """Libera el lock (cierra el socket). Para un reinicio limpio: soltar el puerto ANTES de
    relanzar, o la nueva instancia chocaría con la que aún no ha muerto."""
    while _held:
        try:
            _held.pop().close()
        except OSError:
            pass
