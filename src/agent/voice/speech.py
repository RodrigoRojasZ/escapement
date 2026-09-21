"""Habla en streaming: parte la respuesta en frases y las dice mientras el modelo sigue escribiendo.

Sin esto el daemon espera la respuesta COMPLETA antes de sintetizar la primera sílaba: en una
respuesta de quince segundos, quince segundos de silencio. Aquí el texto se corta en frases en
cuanto llegan los deltas del stream y una cola las reproduce en orden, en un hilo aparte, mientras
el modelo sigue generando — la latencia percibida pasa a ser la de la PRIMERA frase.

Dos piezas, ambas puras respecto del audio (el TTS entra por inyección, así se testean sin
hardware): :class:`SentenceBuffer` decide dónde termina una frase y :class:`SpeechQueue` las
reproduce en orden preservando el barge-in.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable

# Cierres de frase. El salto de línea corta solo (una viñeta o un párrafo ya es una unidad).
_FIN = ".!?…\n"
# Longitud mínima de una frase antes de soltarla: sin este piso, "Sí." o "Claro." se reproducen
# como clips sueltos y el habla suena entrecortada. Un fragmento corto NO se descarta — se pega al
# siguiente hasta llegar al piso (o sale tal cual en el flush final).
_MIN_CHARS = 25
# Abreviaturas cuyo punto NO cierra frase. Lista corta a propósito: el piso de _MIN_CHARS ya
# absorbe la mayoría de los cortes espurios, y una lista larga acabaría cortando de más.
_ABREVS = (
    "sr.",
    "sra.",
    "srta.",
    "dr.",
    "dra.",
    "etc.",
    "ej.",
    "p.ej.",
    "vs.",
    "núm.",
    "ud.",
    "uds.",
    "aprox.",
)


def _es_abreviatura(frase: str) -> bool:
    bajo = frase.lower()
    return any(bajo.endswith(a) for a in _ABREVS)


class SentenceBuffer:
    """Acumula deltas de texto y devuelve las frases completas que van quedando.

    El corte exige que el terminador venga seguido de un espacio (o sea un salto de línea):
    así "3.5" y "p.ej" no parten la frase a mitad. Si el terminador es el último carácter
    recibido, espera más texto — todavía podría venir un dígito detrás.
    """

    def __init__(self) -> None:
        self._buf = ""

    def _corte(self, texto: str) -> int:
        """Índice EXCLUSIVO donde termina la primera frase completa de ``texto``; -1 si no hay."""
        i, n = 0, len(texto)
        while i < n:
            ch = texto[i]
            if ch not in _FIN:
                i += 1
                continue
            fin = i + 1
            if ch != "\n":
                if fin >= n:
                    return -1  # el terminador cierra el buffer: puede seguir un dígito ("3.")
                if not texto[fin].isspace():
                    i += 1  # "3.5", "p.ej" -> no es cierre de frase
                    continue
            cand = texto[:fin].strip()
            if len(cand) >= _MIN_CHARS and not _es_abreviatura(cand):
                return fin
            i += 1  # demasiado corta o abreviatura: se pega a la frase siguiente
        return -1

    def feed(self, delta: str) -> list[str]:
        """Agrega un delta del stream y devuelve las frases que quedaron completas (puede ser []).

        Args:
            delta: fragmento de texto tal como lo emite el stream del modelo; puede ser
                media palabra, varias frases o cadena vacía.
        """
        self._buf += delta
        frases: list[str] = []
        while (fin := self._corte(self._buf)) > 0:
            frases.append(self._buf[:fin].strip())
            self._buf = self._buf[fin:].lstrip()
        return [f for f in frases if f]

    def flush(self) -> str:
        """Devuelve lo que quede sin cerrar (la última frase sin punto) y vacía el buffer."""
        resto, self._buf = self._buf.strip(), ""
        return resto


class SpeechQueue:
    """Cola que reproduce frases en orden en un hilo aparte, cancelable por barge-in.

    Encolar no bloquea: el turno puede seguir consumiendo el stream mientras se habla. Si una
    frase se corta (el usuario pulsó la hotkey), la cola se vacía y todo lo que se encole
    después es no-op — lo que Escapement iba a decir ya no viene al caso.

    Args:
        hablar: reproduce UNA frase y devuelve True si fue interrumpida — misma semántica que
            ``tts.speak(texto, stop=...)``. Se llama SIEMPRE desde el hilo de la cola.
    """

    def __init__(self, hablar: Callable[[str], bool]) -> None:
        self._hablar = hablar
        self._cola: queue.Queue[str | None] = queue.Queue()
        self.interrumpido = False
        self._hilo = threading.Thread(target=self._loop, daemon=True, name="escapement-tts")
        self._hilo.start()

    def say(self, frase: str) -> None:
        """Encola una frase. No-op si está vacía o si ya hubo barge-in."""
        if frase.strip() and not self.interrumpido:
            self._cola.put(frase)

    def _vaciar(self) -> None:
        """Descarta lo pendiente conservando el centinela de cierre (si ya estaba encolado)."""
        cierre = False
        try:
            while True:
                if self._cola.get_nowait() is None:
                    cierre = True
        except queue.Empty:
            pass
        if cierre:
            self._cola.put(None)

    def _loop(self) -> None:
        while True:
            item = self._cola.get()
            if item is None:
                return
            if self.interrumpido:
                continue  # drena sin hablar
            try:
                if self._hablar(item):
                    self.interrumpido = True
                    self._vaciar()
            except Exception:
                pass  # una frase que no se pudo reproducir no tumba el turno

    def close(self, timeout: float | None = None) -> bool:
        """Cierra la cola y espera a que termine de hablar lo encolado.

        Idempotente: llamarla otra vez sobre una cola ya cerrada retorna de inmediato.

        Args:
            timeout: segundos máximos de espera; ``None`` (default) = sin límite.

        Returns:
            True si la reproducción fue interrumpida por barge-in.
        """
        self._cola.put(None)
        self._hilo.join(timeout)
        return self.interrumpido

    def cancel(self, timeout: float | None = None) -> None:
        """Aborta la cola sin esperar lo pendiente (el turno se rompió o lo cancelaron).

        A diferencia de :meth:`close`, descarta lo encolado en vez de decirlo: si el turno no
        llegó a completarse, seguir hablando su respuesta a medias no viene al caso. La frase
        que ya esté sonando termina — el audio solo se corta desde dentro, por la condición de
        ``stop`` del TTS.

        Args:
            timeout: segundos máximos de espera a que el hilo termine; ``None`` = sin límite.
        """
        self.interrumpido = True  # frena lo pendiente y hace no-op los say() posteriores
        self._vaciar()
        self.close(timeout)
