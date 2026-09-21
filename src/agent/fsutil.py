"""Escritura atómica de archivos de estado: write-temp + os.replace (nunca deja un archivo a medias).

Los archivos reanudables de Escapement (``plan.json``, ``state.json``) se reescriben enteros tras cada
paso. Un ``write_text`` directo que se interrumpe a mitad deja JSON truncado -> el plan/estado queda
irreanudable (el bug del informe truncado). Escribir a un temporal y luego ``os.replace`` lo hace
atómico dentro del mismo filesystem: o queda el contenido viejo íntegro, o el nuevo íntegro, nunca a
medias. Sin efectos secundarios al importar.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def write_text_atomic(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Escribe ``text`` en ``path`` de forma atómica (temp en el mismo dir + ``os.replace``).

    Args:
        path: destino final; su directorio se crea si falta.
        text: contenido completo a escribir.
        encoding: codificación del texto (por defecto ``utf-8``).

    El temporal lleva el PID en el nombre para que dos procesos concurrentes (p.ej. el daemon de voz
    y un ``ejecutar``) no colisionen en el mismo temporal. ``os.replace`` es atómico en POSIX y Windows
    siempre que temp y destino estén en el mismo filesystem (lo están: mismo directorio).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding=encoding)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)  # limpia el temporal si os.replace no llegó a consumirlo


def _try_lock(fd: int) -> bool:
    """Intenta el lock exclusivo no-bloqueante del OS sobre ``fd``. True si lo adquirió."""
    try:
        if os.name == "nt":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    try:
        if os.name == "nt":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass  # el lock del OS muere con el file descriptor de todos modos


@contextmanager
def locked(path: Path, timeout: float = 10.0) -> Iterator[None]:
    """Lock exclusivo inter-proceso sobre ``path`` (serializa read-modify-write concurrentes).

    Usa un archivo hermano ``<name>.lock`` con lock del OS (``msvcrt.locking`` en Windows,
    ``flock`` en POSIX): si el proceso muere con el lock tomado, el OS lo libera solo — no
    hay locks huérfanos que romper. Dos escritores (daemon de voz + REPL + orquestador)
    sobre ``state.json`` ya no se pisan el load-modify-save (H8).

    Args:
        path: archivo protegido; el lock vive en ``<path>.lock`` junto a él.
        timeout: segundos máximos esperando el lock antes de rendirse.

    Raises:
        TimeoutError: si el lock no se pudo adquirir dentro de ``timeout``.
    """
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT)
    try:
        deadline = time.monotonic() + timeout
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"no se pudo adquirir el lock {lock_path} en {timeout}s")
            time.sleep(0.05)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)
