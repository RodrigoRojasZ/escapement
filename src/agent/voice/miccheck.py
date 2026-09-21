"""Diagnóstico del micrófono: qué dispositivo usa la captura y si tu voz supera el umbral.

Responde a la pregunta "el ícono está en rojo pero Escapement no contesta": la captura
(:mod:`agent.voice.ears`) abre ``sd.InputStream`` **sin** ``device=``, así que graba del
dispositivo de entrada por defecto de Windows, y descarta el turno si ningún frame supera el
umbral RMS adaptativo. Este módulo muestra las dos cosas —dispositivo y nivel— con exactamente
los mismos parámetros que usa la captura real (se importan de ``ears``, no se copian, para que
no se desincronicen).

Uso::

    python -m agent.voice.miccheck                # lista los dispositivos de entrada
    python -m agent.voice.miccheck --grabar 6     # medidor en vivo 6 s + veredicto
    python -m agent.voice.miccheck --grabar 6 --stt   # además transcribe lo grabado
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from agent.voice.ears import _CALIB_FRAMES, _RMS_CEIL, _RMS_FLOOR, SAMPLE_RATE

_FRAME_S = 0.03  # 30 ms, el mismo bloque que lee `ears.record_toggle`
_MUDO = 1e-5  # por debajo de esto el stream no trae señal, es silencio digital


@dataclass(frozen=True)
class Diagnostico:
    """Resultado de analizar una grabación: umbral aplicado, niveles y veredicto."""

    piso: float
    umbral: float
    rms_max: float
    pico: float
    frames_con_voz: int
    frames: int

    @property
    def hubo_voz(self) -> bool:
        """True si la captura real habría dado por buena esta grabación."""
        return self.frames_con_voz > 0

    @property
    def margen_db(self) -> float:
        """Distancia del pico de voz al umbral, en dB. Negativo = se queda corto."""
        import math

        if self.rms_max <= 0 or self.umbral <= 0:
            return float("-inf")
        return 20.0 * math.log10(self.rms_max / self.umbral)

    @property
    def veredicto(self) -> str:
        """Frase accionable: qué está pasando y qué tocar."""
        if self.pico < _MUDO:
            return (
                "MUDO: el stream no trae señal. El dispositivo por defecto no es el micrófono "
                "que usas, o está silenciado/apagado. Revisa Configuración > Sistema > Sonido > "
                "Entrada."
            )
        if not self.hubo_voz:
            return (
                f"BAJO: hay señal pero nunca superó el umbral ({self.margen_db:+.1f} dB). Escapement "
                "descarta este turno sin transcribir. Sube el volumen del micrófono en Windows "
                "(Sonido > Entrada > Propiedades del dispositivo) o acércate al mic."
            )
        if self.margen_db < 6.0:
            return (
                f"JUSTO: superó el umbral por solo {self.margen_db:+.1f} dB. Funciona, pero "
                "cualquier frase suave se va a caer. Conviene subir la ganancia."
            )
        return f"OK: voz clara, {self.margen_db:+.1f} dB sobre el umbral en {self.frames_con_voz} de {self.frames} frames."


def analizar(rms: list[float]) -> Diagnostico:
    """Aplica a una serie de RMS la misma calibración/umbral que ``ears.record_toggle``.

    Args:
        rms: RMS de cada frame de 30 ms, en orden. Los primeros ``ears._CALIB_FRAMES``
            (~300 ms) se usan para medir el piso de ruido y NO cuentan como voz, igual que en
            la captura real.

    Returns:
        El :class:`Diagnostico` correspondiente. Con menos frames que la calibración, el umbral
        queda en ``_RMS_CEIL`` (el valor exigente con que arranca la captura) y no hay voz.
    """
    pico = max(rms, default=0.0)
    if len(rms) < _CALIB_FRAMES:
        return Diagnostico(
            piso=0.0, umbral=_RMS_CEIL, rms_max=pico, pico=pico, frames_con_voz=0, frames=len(rms)
        )
    calib = rms[:_CALIB_FRAMES]
    resto = rms[_CALIB_FRAMES:]
    piso = min(calib)
    umbral = min(max(_RMS_FLOOR, 3.0 * piso), _RMS_CEIL)
    return Diagnostico(
        piso=piso,
        umbral=umbral,
        rms_max=max(resto, default=0.0),
        pico=pico,
        frames_con_voz=sum(1 for r in resto if r >= umbral),
        frames=len(resto),
    )


def _barra(rms: float, umbral: float, ancho: int = 44) -> str:
    """Medidor de texto con una marca ``|`` en la posición del umbral."""
    tope = 0.25  # full-scale útil de voz; por encima ya está saturando
    lleno = min(ancho, int(ancho * min(rms, tope) / tope))
    marca = min(ancho - 1, int(ancho * min(umbral, tope) / tope))
    celdas = ["█" if i < lleno else "·" for i in range(ancho)]
    if celdas[marca] == "·":
        celdas[marca] = "|"
    return "".join(celdas)


def listar_dispositivos() -> None:
    """Imprime los dispositivos de entrada y marca el que usará la captura."""
    import sounddevice as sd

    try:
        defecto = sd.query_devices(kind="input")
        idx = sd.default.device[0]
        print(f"Entrada por defecto (la que usa Escapement): [{idx}] {defecto['name']}")
        print(f"  canales={defecto['max_input_channels']}  sr={defecto['default_samplerate']:.0f}")
    except Exception as exc:  # pragma: no cover - depende del host de audio
        print(f"No hay dispositivo de entrada por defecto: {exc}")
    print("\nTodas las entradas disponibles:")
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            api = sd.query_hostapis(d["hostapi"])["name"]
            print(f"  [{i:3d}] {d['name']}  (ch={d['max_input_channels']}, {api})")
    print(
        "\nEscapement NO permite elegir dispositivo: usa el de defecto de Windows. Si el de arriba\n"
        "no es tu micrófono, cámbialo en Configuración > Sistema > Sonido > Entrada."
    )


def medir(segundos: float, device: int | None = None) -> tuple[Diagnostico, list]:
    """Graba del micrófono mostrando el nivel en vivo y devuelve el diagnóstico y el audio.

    Args:
        segundos: duración de la medición.
        device: índice de dispositivo a forzar; ``None`` (default) usa el de Windows, que es
            justamente el que usa la captura real.

    Returns:
        Tupla ``(diagnostico, chunks)``; ``chunks`` son los bloques float32 mono capturados.
    """
    import numpy as np
    import sounddevice as sd

    frame = int(SAMPLE_RATE * _FRAME_S)
    total = int(segundos / _FRAME_S)
    rms: list[float] = []
    chunks: list = []
    umbral = _RMS_CEIL
    print(f"\nGrabando {segundos:.0f} s — habla normal, como le hablarías a Escapement.\n")
    with sd.InputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=frame, device=device
    ) as stream:
        for _ in range(total):
            data, _overflow = stream.read(frame)
            x = data.reshape(-1)
            chunks.append(x)
            r = float(np.sqrt(np.mean(x * x)))
            rms.append(r)
            if len(rms) == _CALIB_FRAMES:
                umbral = min(max(_RMS_FLOOR, 3.0 * min(rms)), _RMS_CEIL)
            estado = "VOZ " if len(rms) > _CALIB_FRAMES and r >= umbral else "    "
            print(f"\r{estado}{_barra(r, umbral)}  rms={r:.4f}", end="", flush=True)
    print("\n")
    return analizar(rms), chunks


def _transcribir(chunks: list) -> None:
    """Cierra el lazo: pasa lo grabado por el mismo STT del daemon e imprime el texto."""
    import tempfile
    from pathlib import Path

    import numpy as np
    import soundfile as sf

    from agent.voice import stt

    audio = np.concatenate(chunks)
    ruta = Path(tempfile.gettempdir()) / "escapement_miccheck.wav"
    sf.write(ruta, audio, SAMPLE_RATE)
    print(f"Transcribiendo {ruta} (carga el modelo, puede tardar la primera vez)...")
    texto = stt.transcribe(ruta)
    print(f"STT dijo: {texto!r}" if texto else "STT no entendió nada (texto vacío).")


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada de ``python -m agent.voice.miccheck``."""
    parser = argparse.ArgumentParser(
        prog="python -m agent.voice.miccheck",
        description="Diagnostica el micrófono que usa la captura de voz de Escapement.",
    )
    parser.add_argument(
        "--grabar",
        type=float,
        metavar="SEGUNDOS",
        help="graba N segundos con medidor en vivo y diagnostica el nivel",
    )
    parser.add_argument(
        "--device", type=int, help="fuerza un índice de dispositivo (default: el de Windows)"
    )
    parser.add_argument(
        "--stt", action="store_true", help="además transcribe lo grabado con el STT local"
    )
    args = parser.parse_args(argv)

    listar_dispositivos()
    if not args.grabar:
        return 0

    diag, chunks = medir(args.grabar, args.device)
    print(f"Piso de ruido : {diag.piso:.4f}")
    print(f"Umbral de voz : {diag.umbral:.4f}  (3x el piso, acotado a [{_RMS_FLOOR}, {_RMS_CEIL}])")
    print(f"Pico de voz   : {diag.rms_max:.4f}  ({diag.margen_db:+.1f} dB respecto al umbral)")
    print(f"Frames con voz: {diag.frames_con_voz} de {diag.frames}")
    print(f"\n{diag.veredicto}\n")
    if args.stt and diag.pico >= _MUDO:
        _transcribir(chunks)
    return 0 if diag.hubo_voz else 1


if __name__ == "__main__":
    raise SystemExit(main())
