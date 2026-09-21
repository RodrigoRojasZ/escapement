"""Tests del gatillo por wake word. Sin micrófono, sin openwakeword real, sin descargas."""

import sys
import types

import numpy as np
import pytest

from agent import config
from agent.voice import wakeword


class _Reloj:
    """time falso: monotonic() controlable; sleep() solo avanza el reloj (no bloquea)."""

    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


class _StreamFake:
    """InputStream falso: cada read() avanza el reloj (simula los 80 ms del frame)."""

    def __init__(self, reloj, paso=0.08):
        self._reloj, self._paso = reloj, paso

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self, n):
        self._reloj.sleep(self._paso)
        return np.zeros((n, 1), dtype="int16"), False


class _DetectorFake:
    def __init__(self, scores):
        self.scores = list(scores)
        self.resets = 0

    def predict(self, _chunk):
        return {"hey_jarvis_v0.1": self.scores.pop(0) if self.scores else 0.0}

    def reset(self):
        self.resets += 1


@pytest.fixture(autouse=True)
def _entorno_aislado(monkeypatch):
    """Sin detector cacheado entre tests y sin acceso al openwakeword real (evita descargas)."""
    wakeword.unload()
    monkeypatch.setitem(sys.modules, "openwakeword", None)
    monkeypatch.setitem(sys.modules, "openwakeword.model", None)
    monkeypatch.setattr(wakeword.ears, "hotkey_pressed", lambda hotkey=None: False)
    yield
    # en teardown _detector puede seguir monkeypatcheado a una lambda (sin cache_clear)
    if hasattr(wakeword._detector, "cache_clear"):
        wakeword.unload()


def _con_stream(monkeypatch, reloj):
    fake_sd = types.SimpleNamespace(InputStream=lambda **_kw: _StreamFake(reloj))
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)


def test_wake_detectada_devuelve_wake_y_resetea(monkeypatch):
    reloj = _Reloj()
    monkeypatch.setattr(wakeword, "time", reloj)
    _con_stream(monkeypatch, reloj)
    det = _DetectorFake([0.1, 0.2, 0.9])
    monkeypatch.setattr(wakeword, "_detector", lambda: det)
    monkeypatch.setattr(config, "VOICE_WAKE_THRESHOLD", 0.5)
    assert wakeword.wait_for_trigger(timeout=None) == "wake"
    assert det.resets == 1  # limpia buffers para no re-disparar con la misma frase


def test_score_bajo_expira_en_timeout(monkeypatch):
    reloj = _Reloj()
    monkeypatch.setattr(wakeword, "time", reloj)
    _con_stream(monkeypatch, reloj)
    monkeypatch.setattr(wakeword, "_detector", lambda: _DetectorFake([]))  # siempre 0.0
    monkeypatch.setattr(config, "VOICE_WAKE_THRESHOLD", 0.5)
    assert wakeword.wait_for_trigger(timeout=1.0) == "timeout"


def test_hotkey_gana_durante_la_deteccion(monkeypatch):
    reloj = _Reloj()
    monkeypatch.setattr(wakeword, "time", reloj)
    _con_stream(monkeypatch, reloj)
    det = _DetectorFake([0.9])  # detectaría, pero la hotkey se chequea primero
    monkeypatch.setattr(wakeword, "_detector", lambda: det)
    monkeypatch.setattr(wakeword.ears, "hotkey_pressed", lambda hotkey=None: True)
    assert wakeword.wait_for_trigger(timeout=None) == "hotkey"
    assert det.scores == [0.9]  # predict nunca corrió


def test_sin_detector_degrada_a_solo_hotkey(monkeypatch):
    reloj = _Reloj()
    monkeypatch.setattr(wakeword, "time", reloj)
    monkeypatch.setattr(wakeword, "_detector", lambda: None)
    assert wakeword.wait_for_trigger(timeout=0.5) == "timeout"
    monkeypatch.setattr(wakeword.ears, "hotkey_pressed", lambda hotkey=None: True)
    assert wakeword.wait_for_trigger(timeout=0.5) == "hotkey"


def test_stream_roto_degrada_a_solo_hotkey(monkeypatch):
    reloj = _Reloj()
    monkeypatch.setattr(wakeword, "time", reloj)

    def _explota(**_kw):
        raise OSError("sin micrófono")

    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(InputStream=_explota))
    monkeypatch.setattr(wakeword, "_detector", lambda: _DetectorFake([0.9]))
    monkeypatch.setattr(wakeword.ears, "hotkey_pressed", lambda hotkey=None: True)
    assert wakeword.wait_for_trigger(timeout=1.0) == "hotkey"


def test_detector_sin_openwakeword_es_none():
    # el fixture ya bloqueó el import: la degradación se cachea y no lanza
    assert wakeword._detector() is None
    assert wakeword._detector() is None  # segunda llamada: cacheada, tampoco lanza
