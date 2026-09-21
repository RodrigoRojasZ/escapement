"""Daemon de voz residente: idle ⇄ active con hilo conversacional persistente.

Arranca ligero: solo el hook F2 escuchando (sin modelos ni client → ~0 recursos). Al pulsar F2
carga STT/TTS y REANUDA el hilo por su ``session_id`` (persistido en disco → sobrevive reinicios).
Tras ``FREEZE_AFTER`` sin F2, descarga los modelos y cierra el client (el hilo queda en disco);
el próximo F2 lo retoma. Un comando de voz de ``_NEW_TOPIC``, o ``NEW_THREAD_AFTER`` de
inactividad, empiezan un hilo nuevo. Defaults: congelar 10 min, hilo nuevo a las 12 h.

Con ``config.VOICE_WAKE_WORD`` configurada, el gatillo de turno acepta ADEMÁS la wake word
(manos libres, openWakeWord en CPU): el micrófono queda abierto mientras se espera, pero el
resto del ciclo (congelar, hilo persistente, barge-in) es idéntico.
"""

from __future__ import annotations

import asyncio
import gc
import json
import re
import time
from collections.abc import Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLINotFoundError,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
)

from agent import bus, cancel, config, convlog
from agent.router import ask_local, route
from agent.voice import ears, speech, stt, tts, wakeword
from agent.voice._nowindow import suppress_child_consoles
from agent.voice.controls import CONTROLS
from agent.voice.hotkey import LATCH
from agent.voice.interaction import VoiceGate

_SESSION_FILE = config.DATA_DIR / "voice_session.json"
FREEZE_AFTER = 600  # s sin F2 -> descargar modelos + cerrar client (el hilo se preserva)
NEW_THREAD_AFTER = 12 * 3600  # s de inactividad -> hilo nuevo en la próxima activación
_NEW_TOPIC = re.compile(
    r"\b(nuevo tema|olvida( eso| todo)?|empecemos de (cero|nuevo)|borra el (chat|contexto)|"
    r"reinicia (el )?(chat|contexto))\b",
    re.IGNORECASE,
)
_EXIT = {"salir", "adios", "adiós", "termina", "apágate", "apagate", "detente"}
# Tope de espera al abortar la cola de habla en un turno roto: si el TTS se cuelga, el daemon no
# se queda esperándolo (el hilo es daemon, muere con el proceso).
_TTS_CLOSE_TIMEOUT = 5.0
# Cadencia con la que se vigila la hotkey mientras el modelo responde. Es un sondeo barato (un
# flag en memoria) y marca la latencia percibida al interrumpir: 80 ms se sienten instantáneos.
_SONDEO_HOTKEY = 0.08
# Tope de espera al turno interrumpido: tras `client.interrupt()` el stream debe cerrarse solo.
# Si el runtime no coopera, se cancela la tarea y se sigue — el usuario ya está hablando.
_INTERRUPT_TIMEOUT = 5.0


def _fusionar(previo: str, actual: str) -> str:
    """Une lo dicho en un turno que el usuario cortó con lo que acaba de decir.

    Cuando se pulsa F2 mientras Escapement transcribe, piensa o habla, el turno en curso se corta
    pero su texto NO se tira: el usuario está completando o corrigiendo la misma idea, no
    empezando de cero. Se acumula y viaja junto al turno siguiente.

    Args:
        previo: texto arrastrado de los turnos cortados (``""`` si no hay).
        actual: transcripción del turno recién capturado.

    Returns:
        Ambos textos en orden, separados por un punto cuando el previo no traía cierre — así
        el modelo lee dos frases y no una oración pegada sin sentido.
    """
    previo, actual = previo.strip(), actual.strip()
    if not previo or not actual:
        return previo or actual
    sep = " " if previo[-1] in ".!?…,;:" else ". "
    return f"{previo}{sep}{actual}"


def _load_session() -> tuple[str | None, float]:
    """(session_id, última_actividad_epoch) persistidos; (None, 0.0) si no hay o pasaron >12 h."""
    if _SESSION_FILE.exists():
        try:
            data = json.loads(_SESSION_FILE.read_text(encoding="utf-8"))
            sid, last = data.get("session_id"), float(data.get("last_activity", 0.0))
            if sid and (time.time() - last) > NEW_THREAD_AFTER:
                return None, 0.0  # inactividad larga -> hilo nuevo
            return sid, last
        except Exception:
            pass
    return None, 0.0


def _save_session(session_id: str | None, last_activity: float) -> None:
    _SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SESSION_FILE.write_text(
        json.dumps({"session_id": session_id, "last_activity": last_activity}), encoding="utf-8"
    )


def _unload_models() -> None:
    """Descarga STT/TTS de memoria/VRAM (el daemon en idle no debe ocupar recursos)."""
    stt.unload()
    tts.unload()
    gc.collect()


def _barge_in_pedido() -> bool:
    """True si la hotkey está pidiendo interrumpir el turno en curso.

    ``ears.hotkey_solicitada`` sola no alcanza: la confirmación hablada del gate de permisos abre
    el micrófono DENTRO del turno y le pide al usuario que pulse F2 para contestar. Esa pulsación
    llegaría aquí como barge-in y abortaría justamente el turno que estaba pidiendo el permiso,
    con el gate todavía esperando respuesta. Mientras el micrófono esté reservado
    (:func:`~agent.voice.ears.escucha_exclusiva`), F2 es la respuesta de ese flujo, no una
    interrupción.
    """
    return not ears.escucha_reservada() and ears.hotkey_solicitada()


def _stop_cb() -> Callable[[], bool] | None:
    """Condición de corte del TTS: la hotkey si el barge-in está activo, ``None`` si no.

    Usa :func:`_barge_in_pedido` (tecla presionada O pulsación pendiente en el latch, salvo con
    el micrófono reservado): un toque corto entre dos sondeos del TTS también corta, sin exigir
    que el usuario mantenga F2.
    """
    return _barge_in_pedido if config.VOICE_BARGE_IN else None


def _say(text: str) -> bool:
    """Dice ``text`` por TTS; True si el barge-in lo cortó.

    Publica ``voice.barge_in`` en ese caso: el usuario quiere hablar, la señal queda en el bus y
    el loop vuelve de inmediato a escuchar (la pulsación quedó en el latch). El caller usa el
    retorno para arrastrar el turno cortado al siguiente. Best-effort: nunca propaga.
    """
    try:
        ears.publicar_captura(ears.CAPTURA_HABLANDO)
        if tts.speak(text, stop=_stop_cb()):
            bus.publish("voice.barge_in", {}, source="voz")
            return True
    except Exception:
        pass
    return False


def _hablar_frase(frase: str) -> bool:
    """Reproduce UNA frase de la cola de streaming; True si el barge-in la cortó.

    Es el callable que recibe :class:`speech.SpeechQueue`: se ejecuta en su hilo, así que no
    puede propagar (una frase fallida no debe tumbar el turno) y no publica en el bus — el
    evento ``voice.barge_in`` lo emite el loop una sola vez al cerrar la cola.
    """
    try:
        ears.publicar_captura(ears.CAPTURA_HABLANDO)  # la primera frase ya suena: fin de "pensando"
        return tts.speak(frase, stop=_stop_cb())
    except Exception:
        return False


def _text_delta(event: dict) -> str:
    """Texto hablable de un stream event del API; ``""`` si el evento no aporta ninguno.

    Solo cuenta ``content_block_delta`` con ``text_delta``: los deltas de razonamiento
    (``thinking_delta``) y de argumentos de tools (``input_json_delta``) NO se hablan.

    Args:
        event: evento crudo del stream de la API (``StreamEvent.event``).
    """
    if event.get("type") != "content_block_delta":
        return ""
    delta = event.get("delta") or {}
    return delta.get("text", "") if delta.get("type") == "text_delta" else ""


async def _consumir(
    client: ClaudeSDKClient, cola: speech.SpeechQueue | None
) -> tuple[str, str | None]:
    """Consume la respuesta del turno y devuelve ``(texto_completo, session_id)``.

    Args:
        cola: cola de habla en streaming; con ella, las frases se van diciendo mientras el
            modelo escribe. ``None`` = sin streaming (el caller habla al final, flujo previo).
    """
    partes: list[str] = []
    buf = speech.SentenceBuffer()
    session_id: str | None = None
    async for msg in client.receive_response():
        if isinstance(msg, StreamEvent):
            if cola is not None and (delta := _text_delta(msg.event)):
                for frase in buf.feed(delta):
                    cola.say(frase)
        elif isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    partes.append(block.text)
                    print(block.text)
                elif isinstance(block, ToolUseBlock):
                    print(f"  · {block.name}")
        elif isinstance(msg, ResultMessage) and msg.session_id:
            session_id = msg.session_id  # id del hilo, para reanudarlo luego
    if cola is not None and (resto := buf.flush()):
        cola.say(resto)  # la última frase suele venir sin punto final
    return " ".join(p.strip() for p in partes).strip(), session_id


def _turno_manos_libres() -> str:
    """Turno manos libres: chime de confirmación ("te escucho") + grabación hasta silencio + STT."""
    tts.chime()
    return ears.listen_hands_free()


def _abrir_turno_en_sesion():
    """Chime de "te escucho" + captura del turno dentro de una sesión abierta.

    Va junto en una sola llamada bloqueante (la despacha el executor): el chime debe sonar
    ANTES de que el micrófono se abra, o quedaría grabado dentro del turno.

    Returns:
        El audio del turno, o ``None`` si la sesión se cerró (ver :func:`ears.record_sesion`).
    """
    tts.chime()
    return ears.record_sesion()


async def _escuchar_en_sesion(loop: asyncio.AbstractEventLoop) -> str | None:
    """Captura y transcribe el turno siguiente de una sesión abierta, sin esperar la hotkey.

    Es el reemplazo de :func:`_esperar_turno` mientras la sesión está abierta: el micrófono se
    reabre solo, así que no hay gatillo que aguardar ni timeout de congelamiento — congelar en
    medio de una conversación viva no tiene sentido, y la propia captura la cierra si nadie habla.

    Args:
        loop: event loop donde despachar el trabajo bloqueante (audio/teclado) a un executor.

    Returns:
        La transcripción (``""`` si el audio no dio texto); ``None`` si la sesión se cerró —
        por un toque de la hotkey o por silencio prolongado.
    """
    audio = await loop.run_in_executor(None, _abrir_turno_en_sesion)
    if audio is None:
        return None
    return await loop.run_in_executor(None, ears.transcribe_audio, audio)


async def _esperar_turno(loop: asyncio.AbstractEventLoop, timeout: float | None) -> str | None:
    """Espera el próximo turno de voz y devuelve su transcripción.

    Sin ``config.VOICE_WAKE_WORD``: espera F2 (comportamiento clásico, intacto). Con wake
    word configurada acepta AMBOS gatillos — F2 (push-to-talk) o la frase (manos libres).

    Args:
        loop: event loop donde despachar el trabajo bloqueante (audio/teclado) a un executor.
        timeout: segundos máximos de espera; ``None`` = sin límite.

    Returns:
        La transcripción (``""`` = nada que procesar en este turno); ``None`` si expiró
        ``timeout`` sin actividad (el caller congela).
    """
    if not config.VOICE_WAKE_WORD:
        return await _capturar_y_transcribir(loop, timeout)
    gatillo = await loop.run_in_executor(None, lambda: wakeword.wait_for_trigger(timeout=timeout))
    if gatillo == "timeout":
        return None
    if gatillo == "wake":
        bus.publish("voice.wake", {"model": config.VOICE_WAKE_WORD}, source="voz")
        return await loop.run_in_executor(None, _turno_manos_libres)
    # "hotkey": F2 ya está presionada. En toggle, record_toggle la toma como la activación y
    # graba de inmediato; en hold, la captura espera el PRÓXIMO evento de tecla (que el
    # auto-repeat dispara enseguida). El timeout corto cubre un toque que ya se soltó en hold,
    # y ese caso vuelve como "" (saltar turno), no como None (que congelaría).
    texto = await _capturar_y_transcribir(loop, 2.0)
    return "" if texto is None else texto  # `or ""` aplastaría el sentinel CANCELADO


async def _capturar_y_transcribir(
    loop: asyncio.AbstractEventLoop, timeout: float | None
) -> str | None:
    """Graba con F2 y transcribe, en dos etapas separadas.

    Van separadas (en vez de un ``ears.listen_once``) porque el STT es una llamada bloqueante y
    no cancelable: partir el trabajo deja al loop volver entre ambas etapas y consultar si el
    usuario ya pidió otro turno, que es lo que permite encadenar sin tragarse la pulsación.

    Returns:
        La transcripción (``""`` si no hubo voz); ``ears.CANCELADO`` si un toque de F2 con el
        micrófono abierto descartó el turno; ``None`` si expiró ``timeout`` esperando F2.
    """
    audio = await loop.run_in_executor(None, lambda: ears.capture(wait_timeout=timeout))
    if audio is ears._TIMEOUT:
        return None
    if audio is ears.CANCELADO:
        return ears.CANCELADO  # no hay audio que transcribir: el usuario se arrepintió
    return await loop.run_in_executor(None, ears.transcribe_audio, audio)


async def _consumir_interrumpible(
    client: ClaudeSDKClient, cola: speech.SpeechQueue | None
) -> tuple[str, str | None, bool]:
    """Consume el turno vigilando la hotkey; la corta si el usuario pulsa F2.

    Es la prioridad de la escucha sobre la respuesta: mientras el modelo escribe o Escapement
    habla, F2 significa "ya entendí / me equivoqué / quiero decir otra cosa", así que se aborta
    el turno —cola de habla incluida— en vez de obligar al usuario a oír el resto. Y si el turno
    había lanzado una corrida del plan, también se le pide parar (:data:`agent.cancel.RUN`):
    abortar solo el turno dejaría el hilo del runner despachando pasos a espaldas del usuario.

    Args:
        client: client conectado con la query ya enviada.
        cola: cola de habla en streaming, o ``None`` si el habla va al final del turno.

    Returns:
        ``(texto_parcial, session_id, interrumpido)``. Con ``interrumpido=True`` el texto es lo
        que alcanzó a llegar: se registra igual en el log, porque el hilo del modelo sí lo vio.
    """
    if not config.VOICE_BARGE_IN:  # sin barge-in, el turno se escucha entero
        return (*(await _consumir(client, cola)), False)

    consumo = asyncio.ensure_future(_consumir(client, cola))
    guardia = asyncio.ensure_future(_esperar_hotkey())
    try:
        await asyncio.wait({consumo, guardia}, return_when=asyncio.FIRST_COMPLETED)
        if consumo.done():
            return (*consumo.result(), False)

        # Cortar el turno no basta si el turno lanzó trabajo pesado: `mcp__orq__ejecutar` corre en
        # un hilo que sobrevive al `interrupt()` y seguiría despachando pasos con el lock tomado.
        # La señal es cooperativa y `ejecutar` la limpia al arrancar, así que pedirla siempre es
        # inocuo cuando no hay ninguna corrida.
        cancel.RUN.pedir(f"cortaste el turno con {config.VOICE_HOTKEY.upper()}")
        if cola is not None:
            cola.cancel(timeout=_TTS_CLOSE_TIMEOUT)  # callar YA: el usuario está hablando encima
        try:
            await client.interrupt()
        except Exception:
            pass  # sin interrupt no hay drama: el stream termina solo y ya cancelamos el habla
        try:
            reply, sid = await asyncio.wait_for(asyncio.shield(consumo), timeout=_INTERRUPT_TIMEOUT)
            return reply, sid, True
        except Exception:
            return "", None, True  # lo que se hubiera dicho ya no viene al caso
    finally:
        for tarea in (consumo, guardia):
            if not tarea.done():
                tarea.cancel()


async def _esperar_hotkey() -> None:
    """Vuelve en cuanto el usuario pide interrumpir el turno (ver :func:`_barge_in_pedido`)."""
    while not _barge_in_pedido():
        await asyncio.sleep(_SONDEO_HOTKEY)


async def run_daemon(build_options: Callable[..., ClaudeAgentOptions]) -> None:
    """Loop residente. ``build_options(voice=True, resume=session_id)`` arma las opciones."""
    suppress_child_consoles()  # el CLI de Claude no debe abrir una consola bajo pythonw
    # Veto de GPU: esta es una sesión POR VOZ (Whisper+Kokoro ocupan la VRAM de la 3050). Marca el
    # proceso para que el orquestador (config.route_step) fuerce 100% online y no colapse los 8GB.
    config.set_voice_session(True)
    loop = asyncio.get_running_loop()
    session_id, last_activity = _load_session()
    client: ClaudeSDKClient | None = None
    last_backend: str | None = None
    gate = VoiceGate()  # permisos hablados con memoria por categoría (aprobar-una-vez)
    pendiente = ""  # texto de turnos que el usuario cortó con F2, a la espera de completarse
    sesion_abierta = False  # con VOICE_OPEN_SESSION, el micrófono se reabre solo tras responder
    try:
        LATCH.start()  # hook único de por vida: ninguna F2 se pierde entre etapas del ciclo
    except Exception:
        pass  # sin hook, la captura vuelve a engancharlo (y fallará ahí, donde se nota)
    key = config.VOICE_HOTKEY.upper()
    print(f"{config.AGENT_NAME} residente en background. [{key}] para hablar.")
    if config.VOICE_WAKE_WORD:
        print(f'Wake word activa: "{config.VOICE_WAKE_WORD}" (manos libres) o [{key}].')
    _say(f"{config.AGENT_NAME} listo.")

    try:
        while True:
            if sesion_abierta:
                # Sesión abierta: el micrófono se reabre solo, sin pedir F2 de nuevo. No se
                # publica `inactivo` — el ciclo no está cerrado, y el parpadeo a azul entre dos
                # turnos de la misma conversación sería mentira.
                text = await _escuchar_en_sesion(loop)
                if text is None:  # F2 con el micrófono abierto, o silencio largo -> CERRAR
                    sesion_abierta = False
                    pendiente = ""  # cerrar es cerrar: una frase a medias ya no viene al caso
                    continue
            else:
                # Esperar F2 (o la wake word). Si estamos ACTIVE (client abierto), con timeout
                # de congelamiento.
                ears.publicar_captura(ears.CAPTURA_INACTIVA)  # ciclo cerrado: espera del gatillo
                timeout = FREEZE_AFTER if client is not None else None
                text = await _esperar_turno(loop, timeout)

                if text is None:  # timeout sin F2 -> CONGELAR (libera recursos, preserva el hilo)
                    await client.disconnect()
                    client = None
                    _unload_models()
                    _save_session(session_id, last_activity)
                    pendiente = ""  # tras diez minutos, una frase a medias ya no viene al caso
                    bus.publish("voice.state", {"state": "congelado"}, source="voz")
                    continue

                if text is ears.CANCELADO:  # F2 con el micrófono abierto -> descartar y CERRAR
                    pendiente = ""  # mismo criterio que en la sesión: cerrar es cerrar
                    continue  # sin abrir sesión: el usuario dijo "déjalo", no "sigamos"

                # El gatillo abrió la conversación: de aquí en más el micrófono se reabre solo
                # hasta que el usuario toque F2 con el micrófono abierto.
                sesion_abierta = config.VOICE_OPEN_SESSION

            # F2 durante la transcripción: el usuario quiere seguir hablando. Se guarda lo dicho
            # y se vuelve a abrir el micrófono de inmediato — la pulsación quedó en el latch, así
            # que la captura de la vuelta siguiente arranca sin esperar nada.
            if ears.hotkey_solicitada():
                pendiente = _fusionar(pendiente, text)
                continue
            text = _fusionar(pendiente, text)
            pendiente = ""
            # Nuevo tema pedido desde el tray: reinicia el hilo (y procesa el turno actual en él).
            if CONTROLS.take_reset():
                if client is not None:
                    await client.disconnect()
                    client = None
                session_id, last_backend = None, None
                gate.reset()
                _save_session(None, time.time())
                if not text:
                    _say("Listo, empecemos de cero.")

            if not text:
                continue
            if CONTROLS.paused:  # escucha en pausa desde el tray: ignora el turno
                sesion_abierta = False  # pausar es dejar de escuchar: cerrar el micrófono abierto
                continue

            print(f"\ntú (voz) › {text}")
            low = text.strip().lower().strip(" .!?¿¡")

            if low in _EXIT:
                _say("Hasta luego.")
                return
            if _NEW_TOPIC.search(low):
                if client is not None:
                    await client.disconnect()
                    client = None
                session_id, last_backend = None, None
                gate.reset()  # nueva tarea: vuelve a exigir confirmación de permisos
                _save_session(None, time.time())
                _say("Listo, empecemos de cero.")
                continue

            ears.publicar_captura(ears.CAPTURA_PENSANDO)
            if route(text, last_backend) == "local":  # trivial -> LLM local, sin tocar el hilo
                reply = await loop.run_in_executor(None, ask_local, text)
                last_backend = "local"
                last_activity = time.time()
                _save_session(session_id, last_activity)
                convlog.record(text, reply, backend="local", session_id=session_id)
                if _say(reply):  # cortó para hablar encima: el turno se arrastra al siguiente
                    pendiente = _fusionar(pendiente, text)
                continue

            # ACTIVAR: abrir el client reanudando el hilo (resume) si estaba idle/congelado.
            if client is None:
                client = ClaudeSDKClient(
                    options=build_options(voice=True, resume=session_id, gate=gate)
                )
                await client.connect()
                bus.publish("voice.state", {"state": "activo"}, source="voz")

            await client.query(text)
            # Con streaming, la cola va hablando por frases mientras el modelo escribe: lo que
            # se oye primero es la PRIMERA frase, no la respuesta entera.
            cola = speech.SpeechQueue(_hablar_frase) if config.VOICE_STREAM_TTS else None
            try:
                reply, sid, interrumpido = await _consumir_interrumpible(client, cola)
                if sid:
                    session_id = sid

                last_backend = "claude"
                last_activity = time.time()
                _save_session(session_id, last_activity)
                convlog.record(text, reply, backend="claude", session_id=session_id)
                if interrumpido:
                    # El usuario cortó para hablar: el habla ya se abortó dentro. Volver arriba
                    # sin cerrar la cola (esperar lo encolado sería justo lo que no quiere). Lo
                    # que pidió se arrastra: quien interrumpe suele estar corrigiendo la misma
                    # petición, no abriendo otra.
                    pendiente = _fusionar(pendiente, text)
                    bus.publish("voice.barge_in", {}, source="voz")
                    continue
            except BaseException:
                # El turno se rompió o lo cancelaron: la cola no puede quedar con su hilo
                # esperando un centinela que ya nadie va a encolar, ni seguir diciendo una
                # respuesta que no llegó a completarse. Incluye CancelledError a propósito.
                if cola is not None:
                    cola.cancel(timeout=_TTS_CLOSE_TIMEOUT)
                raise
            if cola is not None:
                if cola.close():  # espera a que termine de decir lo encolado
                    pendiente = _fusionar(pendiente, text)  # cortó el habla: arrastra el turno
                    bus.publish("voice.barge_in", {}, source="voz")
            elif reply:
                if _say(reply):
                    pendiente = _fusionar(pendiente, text)
    except CLINotFoundError:
        print("No encuentro el runtime de Claude. Verifica tu autenticación.")
    finally:
        bus.publish("voice.state", {"state": "apagado"}, source="voz")
        if client is not None:
            await client.disconnect()
