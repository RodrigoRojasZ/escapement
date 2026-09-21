"""Tests del TTS: padding de audio (fix del corte de la última sílaba) y barge-in.

Sin hardware: sounddevice se inyecta fake en sys.modules y SAPI se reemplaza por un doble.
"""

import sys

import numpy as np
import pytest

from agent.voice import tts


class _FakeStream:
    """Stream de reproducción falso: activo durante ``polls`` consultas, luego termina."""

    def __init__(self, polls):
        self._restantes = polls

    @property
    def active(self):
        if self._restantes <= 0:
            return False
        self._restantes -= 1
        return True


class _FakeSD:
    """Doble de sounddevice: registra llamadas a play/stop/wait."""

    def __init__(self, polls=3):
        self.played = self.stopped = self.waited = False
        self._stream = _FakeStream(polls)

    def play(self, _samples, _sr):
        self.played = True

    def stop(self):
        self.stopped = True

    def wait(self):
        self.waited = True

    def get_stream(self):
        return self._stream


class _FakeSapi:
    """Doble de SAPI.SpVoice: registra los Speak y simula ``pendientes`` polls sin terminar."""

    def __init__(self, pendientes=2):
        self.speaks = []
        self._pendientes = pendientes

    def Speak(self, text, flags=None):  # noqa: N802 - nombre COM real
        self.speaks.append((text, flags))

    def WaitUntilDone(self, _ms):  # noqa: N802 - nombre COM real
        if self._pendientes <= 0:
            return True
        self._pendientes -= 1
        return False


@pytest.fixture
def kokoro_fake(monkeypatch):
    """Fuerza la ruta Kokoro con un sounddevice falso; devuelve el doble para asserts."""
    fake = _FakeSD()
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    monkeypatch.setattr(tts, "_use_kokoro", lambda: True)
    monkeypatch.setattr(tts, "_kokoro_audio", lambda _t: (np.zeros(8, dtype=np.float32), 24000))
    monkeypatch.setattr(tts, "_POLL_S", 0.0)  # tests sin esperas reales
    return fake


# ---------------------------------------------------------------- barge-in (Kokoro)


def test_speak_sin_stop_conserva_comportamiento_previo(kokoro_fake):
    assert tts.speak("hola") is False
    assert kokoro_fake.played and kokoro_fake.waited
    assert not kokoro_fake.stopped


def test_speak_con_stop_true_corta_la_reproduccion(kokoro_fake):
    assert tts.speak("hola", stop=lambda: True) is True
    assert kokoro_fake.stopped
    assert not kokoro_fake.waited  # sondeo, no espera bloqueante


def test_speak_con_stop_false_reproduce_completo(kokoro_fake):
    assert tts.speak("hola", stop=lambda: False) is False
    assert not kokoro_fake.stopped


def test_stop_que_lanza_cuenta_como_no_cortar(kokoro_fake):
    def roto():
        raise RuntimeError("boom")

    assert tts.speak("hola", stop=roto) is False
    assert not kokoro_fake.stopped


def test_sondeo_imposible_degrada_a_espera_bloqueante(kokoro_fake, monkeypatch):
    # get_stream roto: no debe propagar (dispararía el fallback SAPI y sonaría dos veces).
    def sin_stream():
        raise RuntimeError("no stream")

    monkeypatch.setattr(kokoro_fake, "get_stream", sin_stream)
    assert tts.speak("hola", stop=lambda: True) is False
    assert kokoro_fake.waited and not kokoro_fake.stopped


# ---------------------------------------------------------------- barge-in (SAPI fallback)


@pytest.fixture
def sapi_fake(monkeypatch):
    fake = _FakeSapi()
    monkeypatch.setattr(tts, "_use_kokoro", lambda: False)
    monkeypatch.setattr(tts, "_speaker", lambda: fake)
    monkeypatch.setattr(tts, "_POLL_S", 0.0)
    return fake


def test_sapi_sin_stop_habla_sincrono(sapi_fake):
    assert tts.speak("hola") is False
    assert sapi_fake.speaks == [("hola", None)]  # Speak clásico, sin flags


def test_sapi_con_stop_corta_con_purga(sapi_fake):
    assert tts.speak("hola", stop=lambda: True) is True
    assert sapi_fake.speaks == [
        ("hola", tts._SAPI_ASYNC),  # arranca async
        ("", tts._SAPI_ASYNC | tts._SAPI_PURGE),  # corte: purga la cola
    ]


def test_sapi_con_stop_false_termina_completo(sapi_fake):
    assert tts.speak("hola", stop=lambda: False) is False
    assert sapi_fake.speaks == [("hola", tts._SAPI_ASYNC)]


def test_pad_tail_agrega_silencio_al_final():
    s = np.ones(100, dtype=np.float32)
    out = tts._pad_tail(s, 24000, 0.1)
    assert len(out) == 100 + int(24000 * 0.1)
    assert np.all(out[100:] == 0)  # la cola es silencio
    assert np.all(out[:100] == 1)  # el audio original queda intacto
    assert out.dtype == np.float32


def test_pad_tail_cero_no_cambia_el_audio():
    s = np.ones(10, dtype=np.float32)
    assert len(tts._pad_tail(s, 24000, 0.0)) == 10


def test_pad_tail_preserva_dtype():
    s = np.ones(5, dtype=np.int16)
    assert tts._pad_tail(s, 24000, 0.05).dtype == np.int16


# ------------------------------------------------------------------ chime (wake word)


def test_chime_reproduce_un_tono_corto(monkeypatch):
    class _SDChime(_FakeSD):
        def __init__(self):
            super().__init__()
            self.audio = None

        def play(self, samples, sr):
            self.audio = (samples, sr)
            self.played = True

    fake = _SDChime()
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    tts.chime()
    assert fake.played and fake.waited
    samples, sr = fake.audio
    assert sr == tts._CHIME_RATE
    assert len(samples) == int(tts._CHIME_RATE * tts._CHIME_S)
    assert float(np.max(np.abs(samples))) <= 0.2 + 1e-6  # volumen moderado (tolerancia float32)
    assert abs(float(samples[0])) < 1e-6 and abs(float(samples[-1])) < 1e-3  # anti-click


def test_chime_sin_dispositivo_no_lanza(monkeypatch):
    class _SDRoto:
        def play(self, *_a):
            raise OSError("sin dispositivo de salida")

    monkeypatch.setitem(sys.modules, "sounddevice", _SDRoto())
    tts.chime()  # best-effort: no debe propagar
