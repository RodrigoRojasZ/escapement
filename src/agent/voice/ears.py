"""Captura de micrófono: hotkey en modo toggle (F2 abre y un segundo toque cancela, default) o
hold (push-to-talk clásico: mantén F2), manos libres (graba hasta silencio tras la wake word) y
sesión abierta (:func:`record_sesion`: el micrófono se reabre solo turno a turno y F2 cierra).

Con el micrófono abierto F2 siempre significa lo mismo —descartar y cerrar, :data:`CANCELADO`—;
las otras fases del ciclo las maneja el daemon (ver la tabla de F2 por fase en el README).

Las libs de audio (keyboard/sounddevice/soundfile/numpy) se importan dentro de las
funciones para que el modo solo-texto no las cargue.
"""

from __future__ import annotations

import contextlib
import queue
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

from agent import bus, config
from agent.voice import stt
from agent.voice.hotkey import LATCH

SAMPLE_RATE = 16000
_TIMEOUT = object()  # sentinel: la espera de la hotkey expiró (el daemon lo usa para congelar)


class _Cancelado(str):
    """Tipo de :data:`CANCELADO`: un ``str`` vacío que se distingue por identidad."""

    __slots__ = ()


# Sentinel: la hotkey se tocó con el micrófono abierto -> el audio de ese turno se DESCARTA y la
# sesión se cierra. Viaja igual por la captura y por la transcripción: es un str vacío, así que
# quien solo mire el texto lo trata como "nada que procesar", pero el daemon lo distingue con
# `is` para no reabrir el micrófono (ver la tabla de fases de F2 en el README).
CANCELADO = _Cancelado()

# Fases del ciclo de voz publicadas en el bus (topic `voice.capture`) para que la presencia —hoy
# el ícono de bandeja— refleje en qué anda Escapement. Necesario desde que F2 es un interruptor
# (VOICE_PTT_MODE="toggle"): sin tecla presionada nada delata que sigue grabando. Las dos últimas
# las publica el daemon, no la captura: cierran el ciclo para que "inactivo" signifique de verdad
# "no estoy haciendo nada" y no también "estoy pensando" o "estoy hablando".
CAPTURA_ESCUCHANDO = "escuchando"
CAPTURA_TRANSCRIBIENDO = "transcribiendo"
CAPTURA_PENSANDO = "pensando"
CAPTURA_HABLANDO = "hablando"
CAPTURA_INACTIVA = "inactivo"

_fase_lock = threading.Lock()
_fase = CAPTURA_INACTIVA


def fase_actual() -> str:
    """La última fase publicada del ciclo de voz."""
    with _fase_lock:
        return _fase


def _reset_fase() -> None:
    """Vuelve la fase a ``inactivo`` sin publicar nada (solo para tests, como ``bus._reset``)."""
    global _fase
    with _fase_lock:
        _fase = CAPTURA_INACTIVA


def _abrir_escucha() -> None:
    """Marca el inicio de la escucha y adelanta la carga del STT a un hilo aparte.

    Las dos cosas van juntas en todos los modos de captura (toggle, hold y manos libres): el
    micrófono acaba de abrirse, así que en unos segundos habrá audio que transcribir y conviene
    que el modelo ya esté en memoria para entonces. El prewarm no bloquea ni puede fallar hacia
    afuera; si el modelo ya está cargado es un no-op.
    """
    publicar_captura(CAPTURA_ESCUCHANDO)
    stt.prewarm()


def publicar_captura(estado: str) -> bool:
    """Publica una fase del ciclo de voz en el bus, si es distinta de la vigente.

    Args:
        estado: una de las constantes ``CAPTURA_*`` de este módulo.

    Returns:
        True si hubo cambio de fase (y por tanto evento). Repetir la fase vigente es no-op: el
        parpadeo es el enemigo aquí. Antes, la grabación publicaba ``inactivo`` al cerrar el
        micrófono y el STT publicaba ``transcribiendo`` un instante después — con el drenaje de
        la tecla en medio, el ícono se veía volver a azul con el dedo todavía en F2. Ahora cada
        etapa declara su fase y las repetidas no llegan al bus.

    Best-effort: ``bus.publish`` nunca lanza. Se publica con ``journal=False`` — son unos pocos
    eventos por turno, señal efímera de UI; el journal guarda hechos (turnos, planes).
    """
    with _fase_lock:
        global _fase
        if estado == _fase:
            return False
        _fase = estado
    bus.publish("voice.capture", {"state": estado}, source="voz", journal=False)
    return True


_reserva_lock = threading.Lock()
_reservas = 0


@contextlib.contextmanager
def escucha_exclusiva() -> Iterator[None]:
    """Reserva el micrófono: mientras dure, la hotkey es el gatillo de ESTE flujo y de nadie más.

    La usa la confirmación hablada del gate de permisos, que abre el micrófono desde dentro de un
    turno en curso y le pide al usuario que pulse F2 para contestar. Sin la reserva esa misma
    pulsación la ve también la guardia de barge-in del daemon, que aborta el turno que estaba
    pidiendo el permiso — con dos grabadores abiertos a la vez. No alcanza con mirar la fase: la
    espera de la pulsación ocurre ANTES de que la captura publique ``escuchando``, así que la
    guardia (sondeo de 80 ms) dispararía igual en esa ventana. Por eso es un flag explícito, y
    cubre desde antes de la pregunta hasta después de la respuesta.

    Reentrante y seguro entre hilos: cuenta reservas anidadas y solo libera con la última, incluso
    si el bloque sale por excepción.
    """
    global _reservas
    with _reserva_lock:
        _reservas += 1
    try:
        yield
    finally:
        with _reserva_lock:
            _reservas -= 1


def escucha_reservada() -> bool:
    """True mientras algún flujo tenga el micrófono reservado con :func:`escucha_exclusiva`."""
    with _reserva_lock:
        return _reservas > 0


# Endpointing manos libres (record_until_silence): la grabación tras la wake word no tiene
# hotkey que soltar, así que el corte es por energía (RMS) con umbral adaptativo.
_WAKE_MAX_S = 30.0  # tope duro de una intervención manos libres
_WAKE_SILENCE_S = 1.2  # silencio continuo que cierra la grabación
_WAKE_START_TIMEOUT_S = 6.0  # sin voz tras el wake -> abortar (falso positivo del detector)
_RMS_FLOOR = 0.012  # umbral mínimo absoluto de voz (float32, full-scale 1.0)
_RMS_CEIL = 0.05  # tope del umbral adaptativo (micrófono con mucho ruido de fondo)
_CALIB_FRAMES = 10  # ~300 ms iniciales para medir el piso de ruido

# Modo toggle (config.VOICE_PTT_MODE = "toggle"): F2 abre la grabación y otro toque la cancela;
# el turno se cierra y se manda a transcribir solo tras _TOGGLE_SILENCE_S de silencio (mismo
# endpointing RMS del manos libres, más generoso: el hablante activó a propósito y puede pausar
# para pensar) o en los topes — dejar de hablar nunca deja el micrófono abierto sin fin.
_TOGGLE_MAX_S = 90.0  # tope duro de una intervención en toggle
_TOGGLE_SILENCE_S = 2.0  # silencio continuo que cierra la grabación
_TOGGLE_START_TIMEOUT_S = 15.0  # activó pero nunca habló -> cerrar sin transcribir

# Sesión abierta (config.VOICE_OPEN_SESSION): turnos 2..N de una conversación ya iniciada. No hay
# pulsación que esperar —el micrófono se reabre solo tras cada respuesta—, así que la hotkey cambia
# de significado: aquí F2 CIERRA la sesión. La espera sin voz es larga (pausas para pensar entre
# turnos) pero finita: olvidarse de cerrar no deja el micrófono abierto para siempre.
_SESION_MAX_S = 90.0  # tope duro de una intervención dentro de la sesión
_SESION_SILENCE_S = 1.6  # silencio continuo que cierra el turno (no la sesión)
_SESION_ESPERA_S = 45.0  # nadie habló en todo este rato -> cerrar la sesión


def _esperar_pulsacion(key: str, wait_timeout: float | None) -> bool:
    """Bloquea hasta una pulsación de ``key``; False si expiró ``wait_timeout``.

    Delega en :data:`~agent.voice.hotkey.LATCH`, cuyo hook vive todo el proceso: si el usuario
    pulsó mientras se transcribía o mientras Escapement hablaba, esto retorna de inmediato en vez
    de exigirle una pulsación nueva. ``key`` solo se respeta cuando difiere de la del latch
    (caso de tests o de una hotkey ad-hoc), donde se engancha un hook propio y efímero.
    """
    if key == LATCH.key:
        return LATCH.wait(wait_timeout)

    import keyboard

    if wait_timeout is None:
        keyboard.wait(key)
        return True
    pressed = threading.Event()
    handler = keyboard.on_press_key(key, lambda _e: pressed.set())
    try:
        return pressed.wait(wait_timeout)
    finally:
        keyboard.unhook(handler)


def record_while_held(
    hotkey: str | None = None, samplerate: int = SAMPLE_RATE, wait_timeout: float | None = None
):
    """Espera a que se presione la hotkey y graba mientras se mantenga.

    Args:
        wait_timeout: segundos máximos esperando la PRIMERA pulsación; ``None`` = sin límite.

    Returns:
        Audio float32 mono (numpy 1-D); ``None`` si no se capturó nada; ``_TIMEOUT`` si expiró
        ``wait_timeout`` sin pulsación.
    """
    import keyboard
    import numpy as np
    import sounddevice as sd

    key = hotkey or config.VOICE_HOTKEY
    if not _esperar_pulsacion(key, wait_timeout):
        return _TIMEOUT
    q: queue.Queue = queue.Queue()

    def _cb(indata, _frames, _time, _status):
        q.put(indata.copy())

    chunks = []
    _abrir_escucha()
    LATCH.clear()  # la pulsación que activó este turno no debe reabrir el siguiente
    try:
        with sd.InputStream(samplerate=samplerate, channels=1, dtype="float32", callback=_cb):
            while keyboard.is_pressed(key):
                try:
                    chunks.append(q.get(timeout=0.1))
                except queue.Empty:
                    pass
            while not q.empty():
                chunks.append(q.get())
    finally:
        # Con audio, la fase que sigue es el STT: publicarla aquí evita el parpadeo a azul
        # entre cerrar el micrófono y arrancar a transcribir.
        publicar_captura(CAPTURA_TRANSCRIBIENDO if chunks else CAPTURA_INACTIVA)
        LATCH.clear()  # soltar la tecla no es pedir otro turno

    if not chunks:
        return None
    return np.concatenate(chunks, axis=0).reshape(-1)


def record_toggle(
    hotkey: str | None = None, samplerate: int = SAMPLE_RATE, wait_timeout: float | None = None
):
    """La hotkey como interruptor: un toque abre la grabación; el silencio o los topes la cierran
    y la mandan a transcribir; un segundo toque la CANCELA.

    Un toque con el micrófono ya abierto significa lo mismo que dentro de una sesión
    (:func:`record_sesion`): "déjalo, no era esto". El audio de ese turno se descarta y el daemon
    cierra la sesión sin abrir nada — así F2 tiene un solo significado en la fase 🔴 escuchando,
    haya sesión o no.

    Si la tecla YA está presionada al entrar, esa pulsación cuenta como la activación (cubre el
    gatillo "hotkey" del loop de wake word y el retorno inmediato tras un barge-in) — no se
    espera un evento nuevo. La pulsación de activación debe soltarse antes de que un nuevo
    toque cuente como cancelación (así el auto-repeat de mantener la tecla no la dispara), y al
    cancelar se espera la soltada (que la misma pulsación no re-active el siguiente turno).

    Args:
        wait_timeout: segundos máximos esperando la pulsación de ACTIVACIÓN; ``None`` = sin
            límite.

    Returns:
        Audio float32 mono (numpy 1-D); ``None`` si no se detectó voz (el umbral RMS se
        calibra igual que en :func:`record_until_silence`); :data:`CANCELADO` si un segundo
        toque descartó el turno; ``_TIMEOUT`` si expiró ``wait_timeout`` sin pulsación.
    """
    import keyboard
    import numpy as np
    import sounddevice as sd

    key = hotkey or config.VOICE_HOTKEY
    if not keyboard.is_pressed(key) and not _esperar_pulsacion(key, wait_timeout):
        return _TIMEOUT
    frame = int(samplerate * 0.03)  # 30 ms
    chunks: list = []
    calib: list[float] = []
    umbral = _RMS_CEIL  # hasta calibrar, exigente: la fase de calibración no marca voz
    hubo_voz, soltada, cancelado, silencio = False, False, False, 0.0
    t0 = time.monotonic()
    _abrir_escucha()
    LATCH.clear()  # la pulsación que activó este turno no debe reabrir el siguiente
    try:
        with sd.InputStream(
            samplerate=samplerate, channels=1, dtype="float32", blocksize=frame
        ) as stream:
            while True:
                transcurrido = time.monotonic() - t0
                if transcurrido >= _TOGGLE_MAX_S:
                    break
                if not hubo_voz and transcurrido >= _TOGGLE_START_TIMEOUT_S:
                    break
                if not keyboard.is_pressed(key):
                    soltada = True  # la pulsación de activación terminó: armar el corte por toque
                elif soltada:
                    cancelado = True  # flanco de subida tras la soltada: segundo toque, descarta
                    break
                data, _overflow = stream.read(frame)
                x = data.reshape(-1)
                chunks.append(x)
                rms = float(np.sqrt(np.mean(x * x)))
                if len(calib) < _CALIB_FRAMES:
                    calib.append(rms)
                    if len(calib) == _CALIB_FRAMES:
                        umbral = min(max(_RMS_FLOOR, 3.0 * min(calib)), _RMS_CEIL)
                    continue
                if rms >= umbral:
                    hubo_voz, silencio = True, 0.0
                elif hubo_voz:
                    silencio += frame / samplerate
                    if silencio >= _TOGGLE_SILENCE_S:
                        break
    finally:
        # El micrófono ya está cerrado; el drenaje de la tecla de abajo no graba nada. Con voz
        # que sí va a transcribirse, la fase que sigue es el STT: publicarla aquí (y no
        # `inactivo`) evita que el ícono vuelva a azul justo mientras el usuario todavía tiene
        # el dedo sobre F2. Al cancelar no hay STT: ahí `inactivo` es la verdad.
        publicar_captura(CAPTURA_TRANSCRIBIENDO if hubo_voz and not cancelado else CAPTURA_INACTIVA)
    # Drenar la tecla SIEMPRE, no solo tras un corte por toque: si el turno cerró por silencio
    # o por tope con el dedo todavía puesto, dejarla presionada haría que el barge-in del turno
    # siguiente se disparara solo, contra una respuesta que aún no empezó.
    while keyboard.is_pressed(key):
        time.sleep(0.02)
    LATCH.clear()  # el toque que cerró el turno no abre el siguiente
    if cancelado:
        return CANCELADO
    if not hubo_voz:
        return None
    return np.concatenate(chunks)


def record_sesion(hotkey: str | None = None, samplerate: int = SAMPLE_RATE):
    """Graba el siguiente turno de una sesión ya abierta: el micrófono arranca de inmediato.

    Es la captura de los turnos 2..N cuando ``config.VOICE_OPEN_SESSION`` está activa. A
    diferencia de :func:`record_toggle` no espera ninguna pulsación —la sesión ya está abierta—,
    y por eso la hotkey significa lo contrario: un toque con el micrófono abierto CIERRA la
    sesión. El corte del turno es por silencio (``_SESION_SILENCE_S``, más corto que en toggle:
    dentro de una conversación viva las pausas son breves).

    Si la tecla sigue presionada al entrar (el toque de barge-in que cortó la respuesta anterior),
    esa pulsación NO cuenta como cierre: hay que soltarla y volver a tocar. Sin eso, interrumpir a
    Escapement cerraría la sesión en el acto en vez de darle el turno al usuario.

    Args:
        hotkey: tecla de cierre; ``None`` (default) usa ``config.VOICE_HOTKEY``.
        samplerate: frecuencia de muestreo; default ``SAMPLE_RATE`` (16 kHz, lo que espera STT).

    Returns:
        Audio float32 mono (numpy 1-D) con lo dicho en este turno; ``None`` si la sesión debe
        CERRARSE — sea por un toque de la hotkey o porque nadie habló en ``_SESION_ESPERA_S``.
        Ambas causas devuelven lo mismo a propósito: el caller solo necesita saber que se acabó.
        Un cierre por hotkey descarta el audio de este turno (quien toca F2 para cerrar no está
        pidiendo que se procese lo que venía diciendo).
    """
    import keyboard
    import numpy as np
    import sounddevice as sd

    key = hotkey or config.VOICE_HOTKEY
    frame = int(samplerate * 0.03)  # 30 ms
    chunks: list = []
    calib: list[float] = []
    umbral = _RMS_CEIL  # hasta calibrar, exigente: la fase de calibración no marca voz
    hubo_voz, soltada, cerrar, silencio = False, False, False, 0.0
    t0 = time.monotonic()
    _abrir_escucha()
    LATCH.clear()  # el toque que abrió la sesión (o que cortó la respuesta) ya se consumió
    try:
        with sd.InputStream(
            samplerate=samplerate, channels=1, dtype="float32", blocksize=frame
        ) as stream:
            while True:
                transcurrido = time.monotonic() - t0
                if transcurrido >= _SESION_MAX_S:
                    break
                if not hubo_voz and transcurrido >= _SESION_ESPERA_S:
                    cerrar = True  # silencio largo: la conversación terminó sola
                    break
                if not keyboard.is_pressed(key):
                    soltada = True  # la pulsación heredada terminó: armar el cierre por toque
                elif soltada:
                    cerrar = True  # flanco de subida tras la soltada: F2 cierra la sesión
                    break
                data, _overflow = stream.read(frame)
                x = data.reshape(-1)
                chunks.append(x)
                rms = float(np.sqrt(np.mean(x * x)))
                if len(calib) < _CALIB_FRAMES:
                    calib.append(rms)
                    if len(calib) == _CALIB_FRAMES:
                        umbral = min(max(_RMS_FLOOR, 3.0 * min(calib)), _RMS_CEIL)
                    continue
                if rms >= umbral:
                    hubo_voz, silencio = True, 0.0
                elif hubo_voz:
                    silencio += frame / samplerate
                    if silencio >= _SESION_SILENCE_S:
                        break
    finally:
        publicar_captura(CAPTURA_TRANSCRIBIENDO if hubo_voz and not cerrar else CAPTURA_INACTIVA)
    # Mismo drenaje que en toggle: con el dedo todavía puesto, el barge-in del turno siguiente
    # se dispararía solo contra una respuesta que aún no empezó.
    while keyboard.is_pressed(key):
        time.sleep(0.02)
    LATCH.clear()
    if cerrar or not hubo_voz:
        return None
    return np.concatenate(chunks)


def record_until_silence(samplerate: int = SAMPLE_RATE):
    """Graba hasta que el hablante calla (endpointing por energía RMS adaptativa).

    Es la captura del flujo manos libres (wake word): abre el micrófono, espera a que empiece
    la voz (hasta ``_WAKE_START_TIMEOUT_S``) y corta tras ``_WAKE_SILENCE_S`` de silencio
    continuo o al llegar a ``_WAKE_MAX_S``. El umbral de voz se calibra con el piso de ruido
    de los primeros ~300 ms, acotado entre ``_RMS_FLOOR`` y ``_RMS_CEIL``.

    Args:
        samplerate: frecuencia de muestreo; default ``SAMPLE_RATE`` (16 kHz, lo que espera STT).

    Returns:
        Audio float32 mono (numpy 1-D), incluyendo el contexto previo a la voz; ``None`` si
        nunca hubo voz (falso positivo de la wake word).
    """
    import numpy as np
    import sounddevice as sd

    frame = int(samplerate * 0.03)  # 30 ms
    chunks: list = []
    calib: list[float] = []
    umbral = _RMS_CEIL  # hasta calibrar, exigente: la fase de calibración no marca voz
    empezo, silencio, t0 = False, 0.0, time.monotonic()
    _abrir_escucha()
    try:
        with sd.InputStream(
            samplerate=samplerate, channels=1, dtype="float32", blocksize=frame
        ) as stream:
            while True:
                transcurrido = time.monotonic() - t0
                if transcurrido >= _WAKE_MAX_S:
                    break
                if not empezo and transcurrido >= _WAKE_START_TIMEOUT_S:
                    return None
                data, _overflow = stream.read(frame)
                x = data.reshape(-1)
                chunks.append(x)
                rms = float(np.sqrt(np.mean(x * x)))
                if len(calib) < _CALIB_FRAMES:
                    calib.append(rms)
                    if len(calib) == _CALIB_FRAMES:
                        umbral = min(max(_RMS_FLOOR, 3.0 * min(calib)), _RMS_CEIL)
                    continue
                if rms >= umbral:
                    empezo, silencio = True, 0.0
                elif empezo:
                    silencio += frame / samplerate
                    if silencio >= _WAKE_SILENCE_S:
                        break
    finally:
        publicar_captura(CAPTURA_TRANSCRIBIENDO if empezo else CAPTURA_INACTIVA)
    if not empezo:
        return None
    return np.concatenate(chunks)


def hotkey_pressed(hotkey: str | None = None) -> bool:
    """True si la hotkey push-to-talk está presionada en este instante.

    Es la condición de corte que el daemon pasa a ``tts.speak`` (barge-in): F2 mientras
    Escapement habla detiene la reproducción.

    Args:
        hotkey: tecla a chequear; ``None`` (default) usa ``config.VOICE_HOTKEY``.

    Best-effort: si el hook de teclado no está disponible (UAC/RDP, lib ausente), devuelve
    False — el audio simplemente no se corta, nunca lanza.
    """
    try:
        import keyboard

        return bool(keyboard.is_pressed(hotkey or config.VOICE_HOTKEY))
    except Exception:
        return False


def hotkey_solicitada(hotkey: str | None = None) -> bool:
    """True si el usuario está pidiendo el turno: tecla presionada AHORA o pulsación pendiente.

    Es la condición de barge-in. Mirar solo ``hotkey_pressed`` se pierde los toques cortos —
    entre dos sondeos del TTS la tecla puede subir y bajar sin que nadie la vea; el latch, en
    cambio, la retiene. No consume la pulsación: quien abre el micrófono decide eso.

    Args:
        hotkey: tecla a chequear; ``None`` (default) usa ``config.VOICE_HOTKEY``.
    """
    return hotkey_pressed(hotkey) or LATCH.pending()


def capture(hotkey: str | None = None, wait_timeout: float | None = None):
    """Graba un turno con la hotkey, en el modo que fije ``config.VOICE_PTT_MODE``.

    Args:
        hotkey: tecla de captura; ``None`` (default) usa ``config.VOICE_HOTKEY``.
        wait_timeout: segundos máximos esperando la pulsación de activación; ``None`` = sin
            límite.

    Returns:
        Audio float32 mono (numpy 1-D); ``None`` si no se captó voz; :data:`CANCELADO` si la
        hotkey descartó el turno (solo en modo toggle); ``_TIMEOUT`` si expiró ``wait_timeout``.
        Al volver, la fase publicada es ``transcribiendo`` (hay audio que transcribir) o
        ``inactivo`` (no lo hay) — el caller encadena desde ahí sin pasar por azul.
    """
    grabar = record_toggle if config.VOICE_PTT_MODE == "toggle" else record_while_held
    return grabar(hotkey, wait_timeout=wait_timeout)


def transcribe_audio(audio, nombre: str = "ptt") -> str:
    """Transcribe audio ya capturado.

    Args:
        audio: señal float32 mono a ``SAMPLE_RATE``, tal como la devuelve :func:`capture`.
        nombre: sufijo del wav temporal, para no pisar el de otro flujo (``"ptt"`` / ``"wake"``).

    Returns:
        La transcripción (``""`` si el audio venía vacío).

    Deja la fase en ``transcribiendo`` al terminar bien: la siguiente la decide el caller
    (``pensando`` en el daemon, ``inactivo`` en :func:`listen_once`), y así el ícono no
    parpadea entre etapas. Si el STT falla, sí vuelve a ``inactivo`` — no hay etapa siguiente.
    """
    import soundfile as sf

    if audio is None or len(audio) == 0:
        return ""
    publicar_captura(CAPTURA_TRANSCRIBIENDO)
    try:
        wav = Path(tempfile.gettempdir()) / f"{config.AGENT_SLUG}_{nombre}.wav"
        sf.write(str(wav), audio, SAMPLE_RATE)
        return stt.transcribe(wav, vad_filter=True)
    except BaseException:
        publicar_captura(CAPTURA_INACTIVA)
        raise


def listen_once(hotkey: str | None = None, wait_timeout: float | None = None) -> str | None:
    """Graba con la hotkey (toggle o hold según ``config.VOICE_PTT_MODE``) y transcribe.

    Composición de :func:`capture` + :func:`transcribe_audio` que deja el ciclo cerrado en
    ``inactivo``. La usa quien no encadena nada después (p.ej. las confirmaciones habladas de
    ``interaction``); el daemon usa las dos piezas por separado, porque entre grabar y
    transcribir necesita poder atender una F2 nueva.

    Returns: la transcripción; ``""`` si no hubo audio o si la hotkey canceló la captura (para
    quien pregunta algo, cancelar es no contestar); ``None`` si expiró ``wait_timeout``
    esperando la hotkey (el daemon lo usa para congelar por inactividad).
    """
    audio = capture(hotkey, wait_timeout=wait_timeout)
    if audio is _TIMEOUT:
        return None
    if audio is CANCELADO:
        return ""
    try:
        return transcribe_audio(audio)
    finally:
        publicar_captura(CAPTURA_INACTIVA)


def listen_hands_free() -> str:
    """Graba tras la wake word (corte por silencio) y devuelve la transcripción.

    Returns: la transcripción; ``""`` si no hubo voz (falso positivo del detector) — el VAD
    de silero (``vad_filter=True``) limpia además los bordes en la transcripción.

    Igual que :func:`transcribe_audio`, deja la fase en ``transcribiendo`` si hubo audio: el
    daemon publica ``pensando`` a continuación.
    """
    return transcribe_audio(record_until_silence(), nombre="wake")
