"""Wake word con openWakeWord: manos libres — decir la frase abre un turno sin tocar F2.

Solo participa si ``config.VOICE_WAKE_WORD`` no está vacío (el daemon decide). Todo corre
local en CPU (los modelos de openWakeWord pesan <1 MB); el micrófono queda abierto mientras
se espera el gatillo. Best-effort: si openwakeword no está instalado, el modelo no descarga
o el micrófono falla, ``wait_for_trigger`` degrada a esperar solo la hotkey — el daemon
sigue funcionando como push-to-talk clásico.
"""

from __future__ import annotations

import time
from functools import lru_cache

from agent import config
from agent.voice import ears

_CHUNK = 1280  # 80 ms a 16 kHz: el tamaño de frame que espera openWakeWord
_POLL_S = 0.05  # sondeo de la hotkey en el modo degradado (sin detector)


@lru_cache(maxsize=1)
def _detector():
    """Carga (y cachea) el modelo openWakeWord; ``None`` si no se puede (degradación).

    Intenta cargar directo (funciona offline si el modelo ya está descargado); si falta,
    descarga y reintenta UNA vez. El ``None`` también se cachea: un entorno roto no debe
    reintentar la descarga en cada iteración del loop del daemon.
    """
    try:
        from openwakeword.model import Model
    except Exception:
        return None
    nombre = config.VOICE_WAKE_WORD
    for intento in (1, 2):
        try:
            return Model(wakeword_models=[nombre], inference_framework="onnx")
        except Exception:
            if intento == 2:
                return None
            try:
                from openwakeword.utils import download_models

                # nombre preentrenado -> descarga puntual; ruta .onnx custom -> descarga el
                # set completo (trae los modelos de features que el custom necesita).
                download_models(model_names=[] if nombre.endswith(".onnx") else [nombre])
            except Exception:
                return None
    return None


def _escuchar(det, deadline: float | None) -> str:
    """Loop de detección con micrófono abierto. Devuelve ``"wake"``/``"hotkey"``/``"timeout"``."""
    import sounddevice as sd

    with sd.InputStream(
        samplerate=ears.SAMPLE_RATE, channels=1, dtype="int16", blocksize=_CHUNK
    ) as stream:
        while deadline is None or time.monotonic() < deadline:
            if ears.hotkey_pressed():
                return "hotkey"
            data, _overflow = stream.read(_CHUNK)
            score = max(det.predict(data.reshape(-1)).values())
            if score >= config.VOICE_WAKE_THRESHOLD:
                det.reset()  # limpia buffers internos: evita re-disparo con la misma frase
                return "wake"
    return "timeout"


def wait_for_trigger(timeout: float | None = None) -> str:
    """Espera el próximo gatillo de turno: la wake word O la hotkey (F2).

    Args:
        timeout: segundos máximos de espera; ``None`` = sin límite.

    Returns:
        ``"wake"`` (se dijo la frase), ``"hotkey"`` (F2 presionada) o ``"timeout"``.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    det = _detector()
    if det is not None:
        try:
            return _escuchar(det, deadline)
        except Exception:
            pass  # micrófono/stream roto -> degradar a solo-hotkey por esta espera
    while deadline is None or time.monotonic() < deadline:
        if ears.hotkey_pressed():
            return "hotkey"
        time.sleep(_POLL_S)
    return "timeout"


def unload() -> None:
    """Libera el detector cacheado (tests / reconfiguración)."""
    _detector.cache_clear()
