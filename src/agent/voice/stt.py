"""STT local con faster-whisper (GPU con fallback automático a CPU int8).

El modelo se carga perezosamente (``lru_cache``) para no penalizar el modo texto. Modelo,
device y compute_type se leen de ``config.VOICE_STT_*`` (env > escapement.toml > default).

La carga cuesta ~5 s, así que :func:`prewarm` la adelanta a un hilo en segundo plano en cuanto
se abre el micrófono: mientras el usuario habla, el modelo se está cargando en paralelo.
"""

from __future__ import annotations

import os
import threading
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from agent import config

if TYPE_CHECKING:
    from faster_whisper import WhisperModel


def _register_cuda_dlls() -> None:
    """En Windows, expone las DLLs de las libs nvidia-* (cuBLAS/cuDNN/cudart/...) a ctranslate2.

    ctranslate2 no respeta ``add_dll_directory`` de forma fiable en Windows, así que
    además se anteponen los dirs al ``PATH`` del proceso. Debe correr ANTES de importar
    faster_whisper/ctranslate2.
    """
    import sys

    nvidia = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    if not nvidia.is_dir():
        return
    dirs = [str(d) for d in nvidia.glob("*/bin") if d.is_dir()]
    if not dirs:
        return
    os.environ["PATH"] = os.pathsep.join([*dirs, os.environ.get("PATH", "")])
    if hasattr(os, "add_dll_directory"):
        for d in dirs:
            try:
                os.add_dll_directory(d)
            except Exception:
                pass


@lru_cache(maxsize=1)
def _model() -> WhisperModel:
    """Carga (y cachea) el modelo faster-whisper. Descarga en el primer uso.

    Intenta la GPU (según config) y la verifica con una inferencia mínima; si CUDA o
    sus DLLs fallan, cae a CPU int8 para que la voz nunca quede sin STT.
    """
    if config.VOICE_STT_DEVICE == "cuda":
        _register_cuda_dlls()

    import numpy as np
    from faster_whisper import WhisperModel

    if config.VOICE_STT_DEVICE == "cuda":
        try:
            model = WhisperModel(
                config.VOICE_STT_MODEL, device="cuda", compute_type=config.VOICE_STT_COMPUTE
            )
            # verificación real: una inferencia mínima detecta DLLs CUDA faltantes
            list(model.transcribe(np.zeros(16000, dtype="float32"), language="es")[0])
            return model
        except Exception:
            pass  # cae a CPU
    return WhisperModel(config.VOICE_STT_MODEL, device="cpu", compute_type="int8")


# `lru_cache` NO serializa la llamada: dos hilos que entren a la vez ejecutan `_model()` los dos
# y dejarían dos copias del modelo en VRAM (fatal en una GPU de 8 GB). `_carga_lock` es lo que
# permite que el prewarm corra en paralelo al turno sin ese riesgo; `_hilo_lock` protege solo el
# handle del hilo y jamás se retiene durante una carga.
_carga_lock = threading.Lock()
_hilo_lock = threading.Lock()
_hilo: threading.Thread | None = None


def _cargar() -> WhisperModel:
    """El modelo, cargándolo si hace falta. Único punto de entrada a ``_model()``."""
    with _carga_lock:
        return _model()


def cargado() -> bool:
    """True si el modelo ya está en memoria. No dispara la carga: es una consulta barata."""
    return _model.cache_info().currsize > 0


def prewarm() -> bool:
    """Adelanta la carga del modelo a un hilo en segundo plano, sin bloquear al que llama.

    Pensada para el instante en que se abre el micrófono: la carga (~5 s en frío, tras un
    congelamiento del daemon) se solapa con lo que el usuario tarda en hablar, así el turno no
    la paga. Es best-effort e idempotente — si la carga falla aquí, el turno la reintenta y el
    error aparece donde sí se nota.

    Returns:
        True si esta llamada lanzó la carga; False si el modelo ya estaba cargado o si ya había
        un prewarm en curso.
    """
    if cargado():
        return False
    global _hilo
    with _hilo_lock:
        if _hilo is not None and _hilo.is_alive():
            return False
        _hilo = threading.Thread(target=_precargar, name="stt-prewarm", daemon=True)
        _hilo.start()
    return True


def _precargar() -> None:
    """Cuerpo del hilo de prewarm: cargar y tragarse cualquier error."""
    try:
        _cargar()
    except Exception:
        pass


def transcribe(
    wav_path: str | Path,
    *,
    language: str | None = None,
    vad_filter: bool = False,
    initial_prompt: str | None = None,
) -> str:
    """Transcribe un archivo de audio a texto.

    Args:
        wav_path: ruta al audio (WAV/mp3/...; faster-whisper resamplea a 16 kHz).
        language: código ISO (``"es"``/``"en"``); ``None`` = auto-detección (dual es/en).
        vad_filter: recorta silencios con VAD (útil para audio de micrófono).
        initial_prompt: sesga el reconocimiento (p.ej. términos técnicos); ``None`` usa el default.
    """
    segments, _info = _cargar().transcribe(
        str(wav_path),
        language=language if language is not None else config.VOICE_LANG,
        beam_size=1,
        vad_filter=vad_filter,
        initial_prompt=initial_prompt if initial_prompt is not None else config.VOICE_STT_PROMPT,
    )
    return " ".join(seg.text.strip() for seg in segments).strip()


def unload() -> None:
    """Libera el modelo STT de memoria/VRAM (el daemon lo llama al entrar en idle).

    Toma ``_carga_lock``: si hay un prewarm a medio camino, espera a que termine antes de
    limpiar. Sin eso, el hilo dejaría el modelo en la caché justo después del ``cache_clear`` y
    el congelamiento no habría liberado nada.
    """
    with _carga_lock:
        _model.cache_clear()
