"""Evita que los subprocesos hijos abran una consola en Windows (daemon de voz sin ventana).

El Claude Agent SDK arranca el CLI (``claude.cmd``) con ``anyio.open_process`` sin pasar
``CREATE_NO_WINDOW``; bajo ``pythonw`` (el residente no tiene consola) eso hace parpadear una
ventana de CMD vacía por cada conexión del cliente. Parcheamos ``subprocess.Popen`` para
inyectar el flag en win32. Best-effort e idempotente: nunca rompe el arranque.
"""

from __future__ import annotations

import subprocess
import sys

_patched = False


def suppress_child_consoles() -> None:
    """Fuerza ``CREATE_NO_WINDOW`` en todo ``subprocess.Popen`` hijo. No-op fuera de Windows.

    Idempotente: aplicar el parche más de una vez no lo apila. El flag solo oculta la ventana;
    stdin/stdout/stderr (los pipes que usa el SDK) siguen funcionando igual.
    """
    global _patched
    if _patched or sys.platform != "win32":
        return
    flag = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    original_init = subprocess.Popen.__init__

    def _init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | flag
        original_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _init  # type: ignore[method-assign]
    _patched = True
