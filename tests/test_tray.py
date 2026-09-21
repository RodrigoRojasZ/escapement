"""Tests del ícono de bandeja: refleja la fase del ciclo de voz (`voice.capture`). Sin pystray."""

import pytest

from agent import bus, config
from agent.voice import ears, tray


class _IconFake:
    """Sustituto de ``pystray.Icon``: solo guarda lo último que le asignaron."""

    def __init__(self):
        self.icon = "inicial"
        self.title = "inicial"


@pytest.fixture(autouse=True)
def _bus_limpio():
    """Cada test arranca sin suscriptores ni fase heredada (`publicar_captura` es idempotente)."""
    bus._reset()
    ears._reset_fase()
    yield
    bus._reset()
    ears._reset_fase()


def _sin_pillow(monkeypatch):
    """Evita generar imágenes reales: el ícono es el color, que es lo que se verifica."""
    monkeypatch.setattr(tray, "_icon_image", lambda color=tray._COLOR_REPOSO: color)


def test_escuchando_pinta_el_icono_de_rojo(monkeypatch):
    _sin_pillow(monkeypatch)
    icon = _IconFake()
    tray._seguir_captura(icon)

    ears.publicar_captura(ears.CAPTURA_ESCUCHANDO)

    assert icon.icon == tray._FASES[ears.CAPTURA_ESCUCHANDO][0]
    assert "escuchando" in icon.title


def test_transcribiendo_y_vuelta_a_reposo(monkeypatch):
    _sin_pillow(monkeypatch)
    icon = _IconFake()
    tray._seguir_captura(icon)

    ears.publicar_captura(ears.CAPTURA_TRANSCRIBIENDO)
    assert icon.icon == tray._FASES[ears.CAPTURA_TRANSCRIBIENDO][0]

    ears.publicar_captura(ears.CAPTURA_INACTIVA)
    assert icon.icon == tray._COLOR_REPOSO
    assert config.AGENT_NAME in icon.title


@pytest.mark.parametrize(
    "fase", [ears.CAPTURA_PENSANDO, ears.CAPTURA_HABLANDO, ears.CAPTURA_TRANSCRIBIENDO]
)
def test_cada_fase_del_ciclo_tiene_su_color(monkeypatch, fase):
    """Ninguna etapa del turno deja el ícono en azul: azul significa "no estoy haciendo nada"."""
    _sin_pillow(monkeypatch)
    icon = _IconFake()
    tray._seguir_captura(icon)

    ears.publicar_captura(fase)

    assert icon.icon == tray._FASES[fase][0]
    assert icon.icon != tray._COLOR_REPOSO


def test_repetir_la_fase_vigente_no_repinta(monkeypatch):
    """La idempotencia es lo que evita el parpadeo entre etapas que declaran la misma fase."""
    _sin_pillow(monkeypatch)
    icon = _IconFake()
    tray._seguir_captura(icon)

    assert ears.publicar_captura(ears.CAPTURA_TRANSCRIBIENDO) is True
    icon.icon = "centinela"
    assert ears.publicar_captura(ears.CAPTURA_TRANSCRIBIENDO) is False

    assert icon.icon == "centinela"


def test_estado_desconocido_no_toca_el_icono(monkeypatch):
    _sin_pillow(monkeypatch)
    icon = _IconFake()
    tray._seguir_captura(icon)

    bus.publish("voice.capture", {"state": "loquesea"}, journal=False)

    assert icon.icon == "inicial"
    assert icon.title == "inicial"


def test_otros_topics_de_voz_no_tocan_el_icono(monkeypatch):
    # el prefijo suscrito es "voice.capture": voice.state/voice.wake no deben repintar
    _sin_pillow(monkeypatch)
    icon = _IconFake()
    tray._seguir_captura(icon)

    bus.publish("voice.state", {"state": "congelado"}, journal=False)
    bus.publish("voice.wake", {"model": "hey_jarvis"}, journal=False)

    assert icon.icon == "inicial"


def test_icono_roto_no_tumba_al_productor(monkeypatch):
    """Si pystray falla al repintar, publicar sigue siendo seguro (el turno de voz no se cae)."""
    _sin_pillow(monkeypatch)

    class _IconExplosivo:
        @property
        def icon(self):
            return None

        @icon.setter
        def icon(self, _v):
            raise RuntimeError("pystray murió")

    tray._seguir_captura(_IconExplosivo())
    ears.publicar_captura(ears.CAPTURA_ESCUCHANDO)  # no debe lanzar


def test_cancelar_suscripcion_congela_el_icono(monkeypatch):
    _sin_pillow(monkeypatch)
    icon = _IconFake()
    cancelar = tray._seguir_captura(icon)

    cancelar()
    ears.publicar_captura(ears.CAPTURA_ESCUCHANDO)

    assert icon.icon == "inicial"


def test_icon_image_usa_el_color_pedido():
    """El ícono real (con Pillow) se pinta del color de la fase, no del azul fijo."""
    pil = pytest.importorskip("PIL")
    assert pil  # el import es el requisito; la aserción real va sobre el píxel

    rojo = tray._FASES[ears.CAPTURA_ESCUCHANDO][0]
    img = tray._icon_image(rojo)

    assert img.getpixel((32, 32)) == rojo
    assert tray._icon_image().getpixel((32, 32)) == tray._COLOR_REPOSO


# --- Progreso de una corrida del plan en el tooltip (deuda #2) ------------------------------


def test_step_start_muestra_el_avance_en_el_tooltip(monkeypatch):
    """Sin esto una corrida de minutos deja el tooltip clavado en "pensando…"."""
    _sin_pillow(monkeypatch)
    icon = _IconFake()
    tray._seguir_plan(icon)

    bus.publish(
        "plan.step_start",
        {"repo": "r", "id": 2, "tipo": "editar", "accion": "algo", "hechos": 1, "total": 3},
        journal=False,
    )

    assert "1/3" in icon.title
    assert "paso 2" in icon.title and "editar" in icon.title


def test_step_start_no_toca_el_color(monkeypatch):
    """El color lo manda `voice.capture`: dice si el micrófono está abierto, no el runner."""
    _sin_pillow(monkeypatch)
    icon = _IconFake()
    tray._seguir_plan(icon)

    bus.publish(
        "plan.step_start", {"id": 1, "tipo": "investigar", "hechos": 0, "total": 2}, journal=False
    )

    assert icon.icon == "inicial"


def test_plan_done_restaura_el_rotulo_de_la_fase_vigente(monkeypatch):
    _sin_pillow(monkeypatch)
    icon = _IconFake()
    tray._seguir_captura(icon)
    tray._seguir_plan(icon)

    ears.publicar_captura(ears.CAPTURA_PENSANDO)  # el turno lanzó la corrida
    bus.publish(
        "plan.step_start", {"id": 1, "tipo": "editar", "hechos": 0, "total": 2}, journal=False
    )
    assert "0/2" in icon.title

    bus.publish("plan.done", {"repo": "r", "completado": True}, journal=False)

    assert icon.title == tray._rotulo(tray._FASES[ears.CAPTURA_PENSANDO][1])


def test_plan_done_sin_fase_conocida_vuelve_a_listo(monkeypatch):
    """Caso `escapement ejecutar` en el mismo proceso sin ciclo de voz: no dejar un progreso colgado."""
    _sin_pillow(monkeypatch)
    icon = _IconFake()
    tray._seguir_plan(icon)

    bus.publish(
        "plan.step_start", {"id": 1, "tipo": "editar", "hechos": 0, "total": 1}, journal=False
    )
    bus.publish("plan.done", {"repo": "r", "completado": True}, journal=False)

    assert icon.title == tray._rotulo("listo")


def test_plan_step_intermedio_no_pisa_el_progreso(monkeypatch):
    """`plan.step` (paso TERMINADO) llega al mismo prefijo, pero no aporta avance al tooltip."""
    _sin_pillow(monkeypatch)
    icon = _IconFake()
    tray._seguir_plan(icon)

    bus.publish(
        "plan.step_start", {"id": 1, "tipo": "editar", "hechos": 0, "total": 2}, journal=False
    )
    antes = icon.title
    bus.publish("plan.step", {"id": 1, "estado": "hecho"}, journal=False)

    assert icon.title == antes


def test_seguir_plan_ignora_los_topics_de_voz(monkeypatch):
    _sin_pillow(monkeypatch)
    icon = _IconFake()
    tray._seguir_plan(icon)

    bus.publish("voice.capture", {"state": ears.CAPTURA_ESCUCHANDO}, journal=False)

    assert icon.title == "inicial"
