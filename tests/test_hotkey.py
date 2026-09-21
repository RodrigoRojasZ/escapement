"""Tests del latch de hotkey: retiene las pulsaciones fuera de la grabación. Sin teclado real."""

import sys
import threading
import types

import pytest

from agent import config
from agent.voice.hotkey import LATCH, HotkeyLatch


@pytest.fixture(autouse=True)
def _latch_global_limpio():
    """El latch del proceso lo comparten todos los tests: nadie hereda ni deja pulsaciones."""
    LATCH.stop()
    yield
    LATCH.stop()


class _KeyboardFake:
    """`keyboard` de mentira: registra hooks y permite disparar la tecla a mano."""

    def __init__(self):
        self.hooks: list = []
        self.desenganchados: list = []

    def on_press_key(self, key, cb):
        handle = object()
        self.hooks.append((key, cb, handle))
        return handle

    def unhook(self, handle):
        self.desenganchados.append(handle)

    def disparar(self):
        for _key, cb, _h in self.hooks:
            cb(None)


@pytest.fixture
def teclado(monkeypatch):
    fake = _KeyboardFake()
    monkeypatch.setitem(sys.modules, "keyboard", fake)
    return fake


def test_arranca_sin_pulsaciones_pendientes():
    assert HotkeyLatch().pending() is False


def test_press_marca_pendiente_sin_consumir():
    latch = HotkeyLatch()
    latch.press()
    assert latch.pending() is True
    assert latch.pending() is True  # consultar no consume: el daemon puede preguntar varias veces


def test_take_consume_una_sola_vez():
    latch = HotkeyLatch()
    latch.press()
    assert latch.take() is True
    assert latch.take() is False


def test_es_flag_no_contador():
    """Tres pulsaciones seguidas abren UN turno, no tres."""
    latch = HotkeyLatch()
    latch.press()
    latch.press()
    latch.press()
    assert latch.take() is True
    assert latch.pending() is False


def test_clear_descarta_sin_abrir_turno():
    latch = HotkeyLatch()
    latch.press()
    latch.clear()
    assert latch.pending() is False


def test_key_sigue_a_config_si_no_se_fijo(monkeypatch):
    latch = HotkeyLatch()
    monkeypatch.setattr(config, "VOICE_HOTKEY", "f9")
    assert latch.key == "f9"  # el cambio de config no queda congelado en el constructor


def test_key_explicita_tiene_prioridad(monkeypatch):
    latch = HotkeyLatch("f8")
    monkeypatch.setattr(config, "VOICE_HOTKEY", "f9")
    assert latch.key == "f8"


def test_start_engancha_una_sola_vez(teclado):
    latch = HotkeyLatch("f8")
    latch.start()
    latch.start()
    assert [k for k, _cb, _h in teclado.hooks] == ["f8"]


def test_el_hook_marca_la_pulsacion(teclado):
    latch = HotkeyLatch("f8")
    latch.start()
    teclado.disparar()
    assert latch.pending() is True


def test_stop_desengancha_y_limpia(teclado):
    latch = HotkeyLatch("f8")
    latch.start()
    latch.press()
    latch.stop()
    assert teclado.desenganchados == [teclado.hooks[0][2]]
    assert latch.pending() is False
    latch.stop()  # idempotente: un segundo stop no vuelve a desenganchar
    assert len(teclado.desenganchados) == 1


def test_stop_no_propaga_si_el_unhook_falla(monkeypatch):
    """Al cerrar el proceso el hook puede estar muerto; eso no puede tumbar el apagado."""
    fake = _KeyboardFake()
    fake.unhook = lambda _h: (_ for _ in ()).throw(RuntimeError("hook muerto"))
    monkeypatch.setitem(sys.modules, "keyboard", fake)
    latch = HotkeyLatch("f8")
    latch.start()
    latch.stop()
    assert latch.pending() is False


def test_start_propaga_si_falta_la_lib(monkeypatch):
    """Sin `keyboard` no hay captura: silenciarlo dejaría al daemon esperando un turno fantasma."""
    monkeypatch.setitem(sys.modules, "keyboard", None)  # import keyboard -> ImportError
    with pytest.raises(ImportError):
        HotkeyLatch("f8").start()


def test_wait_con_pulsacion_previa_vuelve_sin_enganchar(teclado):
    """El punto del módulo: la F2 pulsada durante el STT abre el turno siguiente al instante."""
    latch = HotkeyLatch("f8")
    latch.press()
    assert latch.wait(timeout=5.0) is True
    assert teclado.hooks == []  # no hizo falta esperar nada
    assert latch.pending() is False  # y la consumió


def test_wait_expira_sin_pulsacion(teclado):
    latch = HotkeyLatch("f8")
    assert latch.wait(timeout=0.01) is False
    assert [k for k, _cb, _h in teclado.hooks] == ["f8"]


def test_wait_vuelve_cuando_llega_la_pulsacion(teclado):
    latch = HotkeyLatch("f8")
    threading.Timer(0.05, teclado.disparar).start()
    assert latch.wait(timeout=5.0) is True
    assert latch.pending() is False  # consumida por el wait


def test_latch_global_es_unico():
    """Como `controls.CONTROLS`: el hook del sistema es uno solo para todo el proceso."""
    from agent.voice import hotkey as modulo

    assert modulo.LATCH is LATCH
    assert isinstance(LATCH, HotkeyLatch)


def test_ears_y_daemon_comparten_el_mismo_latch():
    from agent.voice import daemon, ears

    assert ears.LATCH is LATCH
    assert daemon.LATCH is LATCH


def test_hotkey_solicitada_ve_la_pulsacion_pendiente(monkeypatch):
    """La condición de barge-in: sin tecla presionada, el latch alcanza para pedir el turno."""
    from agent.voice import ears

    monkeypatch.setitem(sys.modules, "keyboard", types.SimpleNamespace(is_pressed=lambda _k: False))
    assert ears.hotkey_solicitada() is False
    LATCH.press()
    assert ears.hotkey_solicitada() is True
    assert LATCH.pending() is True  # consultarla no la consume
