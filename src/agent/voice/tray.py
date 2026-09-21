"""Ícono en la bandeja del sistema para el daemon de voz residente.

Da presencia visible ("Escapement está activo") y un menú de acciones. Corre en modo *detached* (su
propio hilo), así el daemon se queda en el hilo principal —importante porque el TTS SAPI usa COM
(STA) y prefiere el main thread. Las acciones que afectan al loop del daemon (pausar, nuevo tema)
se coordinan por :mod:`agent.voice.controls`; el resto (abrir carpetas, reiniciar, salir) las
resuelve el propio tray. "Salir" cierra el proceso (el daemon persiste el hilo tras cada turno).

El ícono además REFLEJA la fase del ciclo de voz (topic ``voice.capture`` del bus): azul en
reposo, rojo grabando, ámbar transcribiendo, violeta pensando, verde hablando. Con F2 en modo
toggle (default) la tecla no queda presionada, así que el color es la única señal de que el
micrófono sigue abierto — y las dos últimas fases evitan que "azul" mienta diciendo "listo"
mientras el modelo todavía está trabajando.

Y cuando el turno lanza una corrida del plan (que tarda minutos), el tooltip pasa a contar los
pasos (topics ``plan.*``): sin eso "pensando…" se queda quieto un buen rato sin decir en qué va.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from agent import bus, config
from agent.voice import ears

# Color e indicación por fase del ciclo de voz. El color va al ícono; el rótulo, al tooltip.
_COLOR_REPOSO = (70, 100, 210, 255)  # azul
_FASES: dict[str, tuple[tuple[int, int, int, int], str]] = {
    ears.CAPTURA_ESCUCHANDO: ((214, 60, 60, 255), "escuchando…"),  # rojo: micrófono abierto
    ears.CAPTURA_TRANSCRIBIENDO: ((222, 160, 40, 255), "transcribiendo…"),  # ámbar: STT
    ears.CAPTURA_PENSANDO: ((150, 90, 200, 255), "pensando…"),  # violeta: turno del modelo
    ears.CAPTURA_HABLANDO: ((60, 170, 110, 255), "hablando…"),  # verde: TTS sonando
    ears.CAPTURA_INACTIVA: (_COLOR_REPOSO, "listo"),
}


def _rotulo(fase: str = "listo") -> str:
    """Tooltip del ícono: nombre del agente + fase + la hotkey real de config."""
    return f"{config.AGENT_NAME} — {fase} ({config.VOICE_HOTKEY.upper()})"


def _icon_image(color: tuple[int, int, int, int] = _COLOR_REPOSO) -> Any:
    """Ícono simple: círculo de ``color`` con una 'V'.

    Args:
        color: relleno RGBA del círculo; default ``_COLOR_REPOSO`` (azul, sin captura activa).
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((6, 6, 58, 58), fill=color)
    try:
        from PIL import ImageFont

        draw.text((23, 16), "V", fill="white", font=ImageFont.load_default())
    except Exception:
        pass
    return img


def _seguir_captura(icon: Any) -> Callable[[], None]:
    """Suscribe ``icon`` a ``voice.capture``: repinta color y tooltip en cada cambio de fase.

    El callback corre en el hilo del productor (la captura de audio), no en el del tray; pystray
    admite reasignar ``icon.icon``/``icon.title`` desde otro hilo. Si algo falla, ``bus.publish``
    se traga la excepción: un ícono que no repinta nunca puede tumbar la voz.

    Args:
        icon: el ``pystray.Icon`` ya arrancado.

    Returns:
        Función para cancelar la suscripción (el tray vive lo que vive el proceso, así que hoy
        nadie la usa; se devuelve para los tests y para un futuro apagado ordenado).
    """

    def _actualizar(event: bus.Event) -> None:
        fase = _FASES.get(str(event.data.get("state", "")))
        if fase is None:  # estado desconocido: dejar el ícono como está
            return
        color, rotulo = fase
        icon.icon = _icon_image(color)
        icon.title = _rotulo(rotulo)

    return bus.subscribe("voice.capture", _actualizar)


def _seguir_plan(icon: Any) -> Callable[[], None]:
    """Suscribe ``icon`` al avance del runner: el tooltip dice en qué paso va la corrida.

    Una corrida del plan tarda minutos sin decir nada —los ``on_step`` van a stdout, que en el
    daemon nadie mira—, y la fase de voz se queda clavada en "pensando…" todo ese rato. Aquí el
    tooltip cuenta el avance real (``plan.step_start``) y, al terminar (``plan.done``), vuelve al
    rótulo de la fase vigente para no dejar un progreso viejo colgado.

    El color NO se toca: lo manda ``voice.capture``, que es lo que dice si el micrófono está
    abierto. Solo ve las corridas de ESTE proceso (el bus es in-process): un ``escapement ejecutar``
    lanzado en una terminal aparte no mueve el ícono.

    Args:
        icon: el ``pystray.Icon`` ya arrancado.

    Returns:
        Función para cancelar la suscripción (misma convención que :func:`_seguir_captura`).
    """

    def _actualizar(event: bus.Event) -> None:
        if event.topic == "plan.step_start":
            d = event.data
            hechos, total = d.get("hechos", 0), d.get("total", 0)
            icon.title = _rotulo(f"plan {hechos}/{total} · paso {d.get('id')} [{d.get('tipo')}]")
        elif event.topic == "plan.done":
            icon.title = _rotulo(_FASES.get(ears.fase_actual(), (_COLOR_REPOSO, "listo"))[1])

    return bus.subscribe("plan.", _actualizar)


def _open(path: Any) -> None:
    """Abre un archivo o carpeta con la app por defecto de Windows (best-effort)."""
    try:
        os.startfile(str(path))  # noqa: S606 - abrir en el visor del usuario, ruta propia
    except Exception as exc:  # noqa: BLE001 - sin efecto si no hay shell asociado
        print(f"[tray] no pude abrir {path}: {exc}")


def _restart(icon: Any) -> None:
    """Relanza el daemon en un proceso nuevo y cierra el actual (sin Administrador de tareas)."""
    import subprocess
    import sys

    from agent.singleton import release_single_instance

    icon.stop()
    release_single_instance()  # soltar el lock para que la nueva instancia lo tome
    subprocess.Popen([sys.executable, "-m", "agent.cli", "--voz"], close_fds=True)
    os._exit(0)


def start_tray() -> Any | None:
    """Arranca el ícono de bandeja en un hilo aparte (no bloquea). Devuelve el icon, o None si falla.

    Menú: rótulo de estado, pausar/reanudar escucha y nuevo tema (vía ``controls``), accesos a
    historial/datos/proyecto, reiniciar y salir. El ícono queda suscrito a ``voice.capture``
    (ver :func:`_seguir_captura`), así el color delata si el micrófono está abierto, y a
    ``plan.*`` (ver :func:`_seguir_plan`) para que una corrida larga del runner muestre avance.
    """
    try:
        import pystray

        from agent.voice.controls import CONTROLS

        def _pausa_label(_item: Any) -> str:
            return "Reanudar escucha" if CONTROLS.paused else "Pausar escucha"

        menu = pystray.Menu(
            pystray.MenuItem(f"{config.AGENT_NAME} — F2 para hablar", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(_pausa_label, lambda _i, _it: CONTROLS.toggle_pause()),
            pystray.MenuItem("Nuevo tema", lambda _i, _it: CONTROLS.request_reset()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Ver historial",
                lambda _i, _it: _open(
                    config.CONVERSATIONS if config.CONVERSATIONS.exists() else config.DATA_DIR
                ),
            ),
            pystray.MenuItem("Carpeta de datos", lambda _i, _it: _open(config.DATA_DIR)),
            pystray.MenuItem("Abrir proyecto", lambda _i, _it: _open(config.REPO_ROOT)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Reiniciar", lambda icon, _it: _restart(icon)),
            pystray.MenuItem("Salir", lambda icon, _it: (icon.stop(), os._exit(0))),
        )
        icon = pystray.Icon("escapement", _icon_image(), _rotulo(), menu)
        icon.run_detached()  # corre en su propio hilo; deja libre el hilo principal
        _seguir_captura(icon)
        _seguir_plan(icon)
        return icon
    except Exception as exc:  # noqa: BLE001 - sin tray el daemon sigue igual
        print(f"[tray] no disponible ({exc}); Escapement sigue sin ícono.")
        return None
