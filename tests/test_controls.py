"""Tests del canal de control tray<->daemon (pausa + reset del hilo), sin GUI."""

from agent.voice.controls import Controls


def test_pausa_alterna():
    c = Controls()
    assert c.paused is False
    assert c.toggle_pause() is True and c.paused is True
    assert c.toggle_pause() is False and c.paused is False


def test_reset_se_consume_una_sola_vez():
    c = Controls()
    assert c.take_reset() is False  # nada pendiente al inicio
    c.request_reset()
    assert c.take_reset() is True  # consume la señal
    assert c.take_reset() is False  # ya consumida: no se repite
