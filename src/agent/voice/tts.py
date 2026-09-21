"""TTS local. Kokoro-82M (voz natural, kokoro-onnx) por defecto; SAPI como fallback.

Interfaz estable para el resto del código: :func:`speak` reproduce por los parlantes,
:func:`synth_to_wav` escribe un WAV. El engine se elige con ``config.VOICE_TTS_ENGINE``
("kokoro" | "sapi"); si Kokoro falla en runtime, cae a SAPI automáticamente.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

from agent import config

_POLL_S = 0.05  # periodo de sondeo de la condición de corte durante la reproducción (barge-in)


# --------------------------------------------------------------------------- #
# Kokoro (kokoro-onnx): voz neuronal natural, es/en, CPU, sin torch.
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def _kokoro() -> Any:
    from kokoro_onnx import Kokoro

    return Kokoro(str(config.VOICE_KOKORO_ONNX), str(config.VOICE_KOKORO_VOICES))


def _kokoro_audio(text: str):
    """Devuelve (samples float32, sample_rate=24000)."""
    return _kokoro().create(text, voice=config.VOICE_TTS_VOICE, lang=config.VOICE_KOKORO_LANG)


def _pad_tail(samples: Any, sr: int, seconds: float) -> Any:
    """Añade ``seconds`` de silencio al final del audio (evita el corte de la última sílaba).

    ``sd.wait()`` retorna al terminar de alimentar el stream, no cuando el buffer de salida del
    hardware se vació; sin esta cola, la latencia de salida se traga el final de la frase.
    """
    import numpy as np

    arr = np.asarray(samples)
    if seconds <= 0:
        return arr
    tail = np.zeros(int(sr * seconds), dtype=arr.dtype)
    return np.concatenate([arr, tail])


def _use_kokoro() -> bool:
    return config.VOICE_TTS_ENGINE == "kokoro" and Path(config.VOICE_KOKORO_ONNX).exists()


# --------------------------------------------------------------------------- #
# SAPI (fallback nativo Windows)
# --------------------------------------------------------------------------- #
_ES_HINTS = ("spanish", "español", "sabina", "helena", "laura", "-es")


def _pick_voice(sp: Any) -> str | None:
    for v in sp.GetVoices():
        d = v.GetDescription()
        if any(k in d.lower() for k in _ES_HINTS):
            sp.Voice = v
            return d
    return None


def _new_sapi() -> Any:
    import win32com.client

    sp = win32com.client.Dispatch("SAPI.SpVoice")
    _pick_voice(sp)
    sp.Rate = config.VOICE_TTS_RATE
    return sp


@lru_cache(maxsize=1)
def _speaker() -> Any:
    return _new_sapi()


# --------------------------------------------------------------------------- #
# Barge-in: reproducción interrumpible por una condición de corte
# --------------------------------------------------------------------------- #
_SAPI_ASYNC = 1  # SVSFlagsAsync: Speak retorna de inmediato, el audio sigue en background
_SAPI_PURGE = 2  # SVSFPurgeBeforeSpeak: descarta lo que esté sonando/en cola


def _stop_pedido(stop: Callable[[], bool]) -> bool:
    """Evalúa la condición de corte; una excepción del callable cuenta como False (no cortar)."""
    try:
        return bool(stop())
    except Exception:
        return False


def _wait_or_stop(sd: Any, stop: Callable[[], bool]) -> bool:
    """Espera el fin de la reproducción sondeando ``stop``; True si se interrumpió.

    Best-effort: si el sondeo no es posible (p.ej. ``get_stream`` falla), degrada a la espera
    bloqueante previa en vez de propagar (propagar dispararía el fallback SAPI y el texto
    sonaría dos veces).
    """
    try:
        stream = sd.get_stream()
        while stream.active:
            if _stop_pedido(stop):
                sd.stop()
                return True
            time.sleep(_POLL_S)
    except Exception:
        try:
            sd.wait()
        except Exception:
            pass
    return False


def _sapi_speak(text: str, stop: Callable[[], bool] | None) -> bool:
    """Habla vía SAPI; con ``stop`` usa modo async + purga para poder cortar. True si se cortó."""
    sp = _speaker()
    if stop is None:
        sp.Speak(text)
        return False
    sp.Speak(text, _SAPI_ASYNC)
    while not sp.WaitUntilDone(int(_POLL_S * 1000)):
        if _stop_pedido(stop):
            sp.Speak("", _SAPI_ASYNC | _SAPI_PURGE)
            return True
    return False


# --------------------------------------------------------------------------- #
# Interfaz pública
# --------------------------------------------------------------------------- #
def speak(text: str, *, stop: Callable[[], bool] | None = None) -> bool:
    """Reproduce texto por los parlantes (bloqueante hasta terminar o hasta ``stop``).

    Args:
        text: lo que se va a decir.
        stop: condición de corte (barge-in): callable sin argumentos sondeado ~20 veces/s
            durante la reproducción; en cuanto devuelve True, el audio se detiene. ``None``
            (default) = no interrumpible, comportamiento previo intacto. Si el callable
            lanza, se trata como False (no corta).

    Returns:
        True si la reproducción fue interrumpida por ``stop``; False si terminó completa
        (siempre False con ``stop=None``).
    """
    if _use_kokoro():
        try:
            import sounddevice as sd

            samples, sr = _kokoro_audio(text)
            sd.play(_pad_tail(samples, sr, config.VOICE_TTS_TAIL_SILENCE), sr)
            if stop is None:
                sd.wait()
                return False
            return _wait_or_stop(sd, stop)
        except Exception:
            pass  # cae a SAPI
    return _sapi_speak(text, stop)


_CHIME_RATE = 24000  # mismo samplerate que Kokoro
_CHIME_S = 0.12  # duración del tono
_CHIME_HZ = 880.0  # la5: agudo corto, se distingue de la voz


def chime() -> None:
    """Tono breve de confirmación ("te escucho") tras detectar la wake word.

    Sintetizado en memoria (~0.12 s, 880 Hz, con fades anti-click): no depende de Kokoro ni
    de SAPI y no toca la VRAM. Best-effort: sin numpy/sounddevice o sin dispositivo de
    salida, no lanza — el turno sigue, solo sin feedback audible.
    """
    try:
        import numpy as np
        import sounddevice as sd

        n = int(_CHIME_RATE * _CHIME_S)
        t = np.arange(n, dtype="float32") / _CHIME_RATE
        tono = (0.2 * np.sin(2 * np.pi * _CHIME_HZ * t)).astype("float32")
        borde = max(1, int(_CHIME_RATE * 0.01))
        tono[:borde] *= np.linspace(0.0, 1.0, borde, dtype="float32")
        tono[-borde:] *= np.linspace(1.0, 0.0, borde, dtype="float32")
        sd.play(tono, _CHIME_RATE)
        sd.wait()
    except Exception:
        pass


def synth_to_wav(text: str, path: str | Path) -> Path:
    """Sintetiza texto a un archivo WAV (para tests / round-trip)."""
    if _use_kokoro():
        try:
            import soundfile as sf

            samples, sr = _kokoro_audio(text)
            sf.write(str(path), samples, sr)
            return Path(path)
        except Exception:
            pass  # cae a SAPI

    import win32com.client

    sp = _new_sapi()  # instancia dedicada para no tocar el estado del speaker
    fs = win32com.client.Dispatch("SAPI.SpFileStream")
    fs.Open(str(path), 3)  # 3 = SSFMCreateForWrite
    sp.AudioOutputStream = fs
    try:
        sp.Speak(text)
    finally:
        fs.Close()
    return Path(path)


def unload() -> None:
    """Libera los engines TTS (Kokoro/SAPI) de memoria (el daemon lo llama al entrar en idle)."""
    _kokoro.cache_clear()
    _speaker.cache_clear()
