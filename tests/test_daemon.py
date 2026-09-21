"""Tests de la lógica persistente del daemon de voz. Sin hardware de audio ni cuota."""

import asyncio
import time

import pytest

from agent import bus, config
from agent.cancel import RUN
from agent.voice import daemon, ears
from agent.voice.hotkey import LATCH


@pytest.fixture(autouse=True)
def _estado_limpio():
    """Latch, fase y señal de cancelación son proceso-globales: ni heredarlos ni dejarlos puestos."""
    LATCH.stop()
    ears._reset_fase()
    RUN.limpiar()
    yield
    LATCH.stop()
    ears._reset_fase()
    RUN.limpiar()


def test_save_load_session(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "_SESSION_FILE", tmp_path / "vs.json")
    daemon._save_session("sess-123", time.time())
    sid, last = daemon._load_session()
    assert sid == "sess-123" and last > 0


def test_load_session_expira_tras_12h(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "_SESSION_FILE", tmp_path / "vs.json")
    daemon._save_session("viejo", time.time() - (13 * 3600))  # hace 13 h
    sid, _ = daemon._load_session()
    assert sid is None  # inactividad larga -> hilo nuevo


def test_load_session_vacio(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "_SESSION_FILE", tmp_path / "noexiste.json")
    assert daemon._load_session() == (None, 0.0)


def test_nuevo_tema_detecta_comandos():
    assert daemon._NEW_TOPIC.search("nuevo tema por favor")
    assert daemon._NEW_TOPIC.search("olvida todo")
    assert daemon._NEW_TOPIC.search("empecemos de cero")


def test_nuevo_tema_no_falso_positivo():
    assert not daemon._NEW_TOPIC.search("cuéntame del proyecto nuevo")


def test_unload_models_sin_cargar_no_falla():
    daemon._unload_models()  # cache_clear sobre lru_cache vacío = no-op seguro


# ---------------------------------------------------------------- barge-in en _say


@pytest.fixture
def bus_aislado(monkeypatch, tmp_path):
    """Bus sin suscriptores heredados y journal en tmp (no ensucia data/events.jsonl)."""
    monkeypatch.setattr(config, "EVENTS", tmp_path / "events.jsonl")
    bus._reset()
    yield
    bus._reset()


def test_say_interrumpido_publica_barge_in(monkeypatch, bus_aislado):
    monkeypatch.setattr(config, "VOICE_BARGE_IN", True)
    monkeypatch.setattr(daemon.tts, "speak", lambda _t, stop=None: True)  # F2 cortó
    visto = []
    bus.subscribe("voice.barge_in", visto.append)
    daemon._say("hola")
    assert len(visto) == 1
    assert visto[0].source == "voz"


def test_say_completo_no_publica_barge_in(monkeypatch, bus_aislado):
    monkeypatch.setattr(config, "VOICE_BARGE_IN", True)
    monkeypatch.setattr(daemon.tts, "speak", lambda _t, stop=None: False)  # terminó entero
    visto = []
    bus.subscribe("voice.barge_in", visto.append)
    daemon._say("hola")
    assert visto == []


def test_say_con_barge_in_pasa_hotkey_como_stop(monkeypatch, bus_aislado):
    monkeypatch.setattr(config, "VOICE_BARGE_IN", True)
    llamadas = []
    monkeypatch.setattr(
        daemon.tts, "speak", lambda text, stop=None: llamadas.append((text, stop)) or False
    )
    daemon._say("hola")
    assert llamadas == [("hola", daemon._barge_in_pedido)]


def test_say_devuelve_si_lo_cortaron(monkeypatch, bus_aislado):
    """El caller usa el retorno para arrastrar el turno cortado al siguiente."""
    monkeypatch.setattr(config, "VOICE_BARGE_IN", True)
    monkeypatch.setattr(daemon.tts, "speak", lambda _t, stop=None: True)
    assert daemon._say("hola") is True
    monkeypatch.setattr(daemon.tts, "speak", lambda _t, stop=None: False)
    assert daemon._say("hola") is False


def test_say_no_propaga_fallos_del_tts(monkeypatch, bus_aislado):
    def _explota(_t, stop=None):
        raise RuntimeError("sin audio")

    monkeypatch.setattr(daemon.tts, "speak", _explota)
    assert daemon._say("hola") is False  # best-effort: el turno sigue vivo


def test_say_sin_barge_in_pasa_stop_none(monkeypatch, bus_aislado):
    monkeypatch.setattr(config, "VOICE_BARGE_IN", False)
    llamadas = []
    monkeypatch.setattr(
        daemon.tts, "speak", lambda text, stop=None: llamadas.append((text, stop)) or False
    )
    daemon._say("hola")
    assert llamadas == [("hola", None)]


# ---------------------------------------------------------------- habla en streaming


def test_stop_cb_devuelve_hotkey_con_barge_in(monkeypatch):
    # `_barge_in_pedido` envuelve `hotkey_solicitada` (no `hotkey_pressed`): un toque corto entre
    # dos sondeos también corta, salvo con el micrófono reservado.
    monkeypatch.setattr(config, "VOICE_BARGE_IN", True)
    assert daemon._stop_cb() is daemon._barge_in_pedido


def test_stop_cb_devuelve_none_sin_barge_in(monkeypatch):
    monkeypatch.setattr(config, "VOICE_BARGE_IN", False)
    assert daemon._stop_cb() is None


# ------------------------------------------------- barge-in vs. confirmación hablada del gate


def test_barge_in_pedido_ve_la_pulsacion(monkeypatch):
    monkeypatch.setattr(daemon.ears, "hotkey_solicitada", lambda: True)
    assert daemon._barge_in_pedido() is True


def test_barge_in_pedido_suprimido_con_el_microfono_reservado(monkeypatch):
    """La F2 con la que contestas un permiso no puede abortar el turno que lo pidió."""
    monkeypatch.setattr(daemon.ears, "hotkey_solicitada", lambda: True)
    with ears.escucha_exclusiva():
        assert daemon._barge_in_pedido() is False
    assert daemon._barge_in_pedido() is True  # liberada la reserva, vuelve a ser interrupción


def test_esperar_hotkey_no_vuelve_con_la_escucha_reservada(monkeypatch):
    """La guardia del turno sigue esperando mientras el gate tiene el micrófono."""
    monkeypatch.setattr(daemon.ears, "hotkey_solicitada", lambda: True)
    monkeypatch.setattr(daemon, "_SONDEO_HOTKEY", 0.001)

    async def _correr():
        with ears.escucha_exclusiva():
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(daemon._esperar_hotkey(), timeout=0.05)
        await asyncio.wait_for(daemon._esperar_hotkey(), timeout=1.0)  # sin reserva, dispara

    asyncio.run(_correr())


def test_text_delta_extrae_texto():
    ev = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hola"}}
    assert daemon._text_delta(ev) == "hola"


def test_text_delta_ignora_razonamiento():
    ev = {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "mmm"}}
    assert daemon._text_delta(ev) == ""


def test_text_delta_ignora_argumentos_de_tools():
    ev = {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "{"}}
    assert daemon._text_delta(ev) == ""


def test_text_delta_ignora_otros_eventos():
    assert daemon._text_delta({"type": "message_start"}) == ""
    assert daemon._text_delta({"type": "content_block_delta"}) == ""


def test_hablar_frase_no_propaga_excepciones(monkeypatch):
    monkeypatch.setattr(config, "VOICE_BARGE_IN", True)

    def boom(_t, stop=None):
        raise RuntimeError("sin dispositivo")

    monkeypatch.setattr(daemon.tts, "speak", boom)
    assert daemon._hablar_frase("hola") is False


def test_hablar_frase_reporta_corte(monkeypatch):
    monkeypatch.setattr(config, "VOICE_BARGE_IN", True)
    monkeypatch.setattr(daemon.tts, "speak", lambda _t, stop=None: True)
    assert daemon._hablar_frase("hola") is True


class _ClientFalso:
    """Client mínimo que reemite una secuencia fija de mensajes."""

    def __init__(self, mensajes):
        self._mensajes = mensajes

    async def receive_response(self):
        for m in self._mensajes:
            yield m


def _stream(texto):
    from claude_agent_sdk import StreamEvent

    return StreamEvent(
        uuid="u",
        session_id="s",
        event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": texto}},
        parent_tool_use_id=None,
    )


def _consumir(mensajes, cola=None):
    async def go():
        return await daemon._consumir(_ClientFalso(mensajes), cola)

    return asyncio.run(go())


def test_consumir_sin_cola_devuelve_texto_y_session_id():
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    msgs = [
        AssistantMessage(content=[TextBlock(text="hola")], model="m"),
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="sess-9",
            total_cost_usd=None,
            usage=None,
            result=None,
        ),
    ]
    assert _consumir(msgs) == ("hola", "sess-9")


def test_consumir_con_cola_habla_por_frases():
    dichas = []
    cola = daemon.speech.SpeechQueue(lambda f: bool(dichas.append(f)))
    msgs = [_stream("Ya revisé el repositorio completo. "), _stream("Falta el último detalle")]
    reply, sid = _consumir(msgs, cola)
    cola.close(timeout=5)
    # Sin AssistantMessage no hay texto acumulado, pero las frases sí se hablaron en orden.
    assert (reply, sid) == ("", None)
    assert dichas == ["Ya revisé el repositorio completo.", "Falta el último detalle"]


def test_consumir_propaga_errores_y_la_cola_se_puede_abortar():
    """Contrato del que depende el `except` del turno: `_consumir` no traga los errores."""

    class _ClientRoto:
        async def receive_response(self):
            raise RuntimeError("conexión caída")
            yield  # pragma: no cover — solo para que la función sea async generator

    dichas = []
    cola = daemon.speech.SpeechQueue(lambda f: bool(dichas.append(f)))
    cola.say("algo que ya no viene al caso")
    with pytest.raises(RuntimeError):
        asyncio.run(daemon._consumir(_ClientRoto(), cola))
    cola.cancel(timeout=daemon._TTS_CLOSE_TIMEOUT)
    assert not cola._hilo.is_alive()  # el hilo no queda esperando un centinela que no llega


def test_consumir_no_habla_razonamiento():
    from claude_agent_sdk import StreamEvent

    dichas = []
    cola = daemon.speech.SpeechQueue(lambda f: bool(dichas.append(f)))
    pensando = StreamEvent(
        uuid="u",
        session_id="s",
        event={"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "x"}},
        parent_tool_use_id=None,
    )
    _consumir([pensando], cola)
    cola.close(timeout=5)
    assert dichas == []


# ---------------------------------------------------------------- wake word en _esperar_turno


def _esperar(timeout):
    async def go():
        return await daemon._esperar_turno(asyncio.get_running_loop(), timeout)

    return asyncio.run(go())


def _captura_falsa(monkeypatch, resultado="audio", texto="hola"):
    """Sustituye captura+STT por dos etapas instantáneas; devuelve la lista de wait_timeout."""
    vistos: list = []

    def _capture(hotkey=None, wait_timeout=None):
        vistos.append(wait_timeout)
        return resultado

    monkeypatch.setattr(daemon.ears, "capture", _capture)
    monkeypatch.setattr(daemon.ears, "transcribe_audio", lambda audio, nombre="ptt": texto)
    return vistos


def test_esperar_turno_sin_wake_captura_y_transcribe(monkeypatch):
    monkeypatch.setattr(config, "VOICE_WAKE_WORD", "")
    vistos = _captura_falsa(monkeypatch)
    assert _esperar(600) == "hola"
    assert vistos == [600]  # el timeout de congelamiento llega intacto (flujo clásico)


def test_esperar_turno_sin_wake_devuelve_none_si_expira(monkeypatch):
    # el sentinel de la captura debe seguir traduciéndose a None (el caller congela)
    monkeypatch.setattr(config, "VOICE_WAKE_WORD", "")
    monkeypatch.setattr(
        daemon.ears, "capture", lambda hotkey=None, wait_timeout=None: ears._TIMEOUT
    )
    monkeypatch.setattr(
        daemon.ears,
        "transcribe_audio",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no hay audio que transcribir")),
    )
    assert _esperar(600) is None


def test_esperar_turno_wake_publica_evento_y_escucha(monkeypatch, bus_aislado):
    monkeypatch.setattr(config, "VOICE_WAKE_WORD", "hey_jarvis")
    monkeypatch.setattr(daemon.wakeword, "wait_for_trigger", lambda timeout=None: "wake")
    monkeypatch.setattr(daemon.tts, "chime", lambda: None)
    monkeypatch.setattr(daemon.ears, "listen_hands_free", lambda: "dime la hora")
    visto = []
    bus.subscribe("voice.wake", visto.append)
    assert _esperar(None) == "dime la hora"
    assert len(visto) == 1
    assert visto[0].data == {"model": "hey_jarvis"}


def test_esperar_turno_wake_timeout_congela(monkeypatch):
    monkeypatch.setattr(config, "VOICE_WAKE_WORD", "hey_jarvis")
    monkeypatch.setattr(daemon.wakeword, "wait_for_trigger", lambda timeout=None: "timeout")
    assert _esperar(600) is None  # None = el caller congela, igual que el flujo clásico


def test_esperar_turno_hotkey_usa_push_to_talk(monkeypatch):
    monkeypatch.setattr(config, "VOICE_WAKE_WORD", "hey_jarvis")
    monkeypatch.setattr(daemon.wakeword, "wait_for_trigger", lambda timeout=None: "hotkey")
    vistos = _captura_falsa(monkeypatch)
    assert _esperar(None) == "hola"
    assert vistos == [2.0]  # espera corta: F2 ya está presionada


def test_esperar_turno_publica_pensando_no_lo_hace_la_captura(monkeypatch):
    """La captura deja la fase en `transcribiendo`; quien pasa a `pensando` es el loop."""
    monkeypatch.setattr(config, "VOICE_WAKE_WORD", "")
    _captura_falsa(monkeypatch)
    ears.publicar_captura(ears.CAPTURA_TRANSCRIBIENDO)
    assert _esperar(600) == "hola"
    assert ears.fase_actual() == ears.CAPTURA_TRANSCRIBIENDO


def _captura_cancelada(monkeypatch):
    """La captura devuelve el sentinel de cancelación; transcribir sería un error."""
    monkeypatch.setattr(
        daemon.ears, "capture", lambda hotkey=None, wait_timeout=None: ears.CANCELADO
    )
    monkeypatch.setattr(
        daemon.ears,
        "transcribe_audio",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("audio descartado, no se transcribe")),
    )


def test_esperar_turno_propaga_cancelado_sin_transcribir(monkeypatch):
    monkeypatch.setattr(config, "VOICE_WAKE_WORD", "")
    _captura_cancelada(monkeypatch)
    assert _esperar(600) is ears.CANCELADO


def test_esperar_turno_hotkey_propaga_cancelado(monkeypatch):
    """El `or ""` del gatillo "hotkey" no puede aplastar el sentinel (es un str vacío)."""
    monkeypatch.setattr(config, "VOICE_WAKE_WORD", "hey_jarvis")
    monkeypatch.setattr(daemon.wakeword, "wait_for_trigger", lambda timeout=None: "hotkey")
    _captura_cancelada(monkeypatch)
    assert _esperar(None) is ears.CANCELADO


def test_esperar_turno_hotkey_soltada_no_congela(monkeypatch):
    # Un toque de F2 que ya se soltó devuelve "" (saltar turno), nunca None (congelaría).
    monkeypatch.setattr(config, "VOICE_WAKE_WORD", "hey_jarvis")
    monkeypatch.setattr(daemon.wakeword, "wait_for_trigger", lambda timeout=None: "hotkey")
    monkeypatch.setattr(
        daemon.ears, "capture", lambda hotkey=None, wait_timeout=None: ears._TIMEOUT
    )
    assert _esperar(None) == ""


# ---------------------------------------------------------------- sesión abierta


def test_abrir_turno_en_sesion_suena_el_chime_antes_de_grabar(monkeypatch):
    """Si el chime sonara con el micrófono ya abierto, quedaría grabado dentro del turno."""
    orden: list[str] = []
    monkeypatch.setattr(daemon.tts, "chime", lambda: orden.append("chime"))
    monkeypatch.setattr(daemon.ears, "record_sesion", lambda: orden.append("grabar") or "audio")
    assert daemon._abrir_turno_en_sesion() == "audio"
    assert orden == ["chime", "grabar"]


def _en_sesion():
    async def go():
        return await daemon._escuchar_en_sesion(asyncio.get_running_loop())

    return asyncio.run(go())


def test_escuchar_en_sesion_transcribe_el_turno(monkeypatch):
    monkeypatch.setattr(daemon.tts, "chime", lambda: None)
    monkeypatch.setattr(daemon.ears, "record_sesion", lambda: "audio")
    monkeypatch.setattr(daemon.ears, "transcribe_audio", lambda audio, nombre="ptt": "dime la hora")
    assert _en_sesion() == "dime la hora"


def test_escuchar_en_sesion_devuelve_none_al_cerrarse(monkeypatch):
    # None de la captura = cerrar la sesión: no hay audio que transcribir
    monkeypatch.setattr(daemon.tts, "chime", lambda: None)
    monkeypatch.setattr(daemon.ears, "record_sesion", lambda: None)
    monkeypatch.setattr(
        daemon.ears,
        "transcribe_audio",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no hay audio que transcribir")),
    )
    assert _en_sesion() is None


class _Guion:
    """Guion del loop de ``run_daemon``: qué devuelve cada espera y en qué orden se pidieron."""

    def __init__(self, gatillos, sesion):
        self.gatillos, self.sesion = list(gatillos), list(sesion)
        self.orden: list[str] = []

    async def esperar_turno(self, _loop, _timeout):
        self.orden.append("gatillo")
        return self.gatillos.pop(0)

    async def escuchar_en_sesion(self, _loop):
        self.orden.append("sesion")
        return self.sesion.pop(0)


class _ControlsFalso:
    """CONTROLS aislado del singleton del tray; ``pausas`` se consume una por turno."""

    def __init__(self, pausas=()):
        self._pausas = list(pausas)

    @property
    def paused(self) -> bool:
        return self._pausas.pop(0) if self._pausas else False

    def take_reset(self) -> bool:
        return False


def _correr_daemon(
    monkeypatch, gatillos, sesion=(), *, open_session=True, pide_turno=(), pausas=(), cortes=()
):
    """Corre ``run_daemon`` con todo lo bloqueante sustituido; termina con un comando de ``_EXIT``.

    ``pide_turno``: respuestas sucesivas de ``hotkey_solicitada`` (F2 durante la transcripción).
    ``cortes``: si el barge-in corta el habla de cada respuesta ("listo", la que da ``ask_local``);
    no cuenta el saludo inicial ni la despedida, que no son turnos.
    Devuelve ``(guion, textos)``, con los textos que llegaron a procesarse como turno.
    """
    import types

    textos: list[str] = []
    respuestas = list(pide_turno)
    interrupciones = list(cortes)

    def _say_falso(texto: str) -> bool:
        return bool(interrupciones.pop(0)) if texto == "listo" and interrupciones else False

    monkeypatch.setattr(config, "VOICE_OPEN_SESSION", open_session)
    monkeypatch.setattr(daemon, "suppress_child_consoles", lambda: None)
    monkeypatch.setattr(daemon.config, "set_voice_session", lambda _v: None)
    monkeypatch.setattr(daemon, "LATCH", types.SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(daemon, "CONTROLS", _ControlsFalso(pausas))
    monkeypatch.setattr(daemon, "_say", _say_falso)
    monkeypatch.setattr(daemon, "_save_session", lambda *_a: None)
    monkeypatch.setattr(daemon, "_load_session", lambda: (None, 0.0))
    monkeypatch.setattr(daemon.convlog, "record", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "route", lambda *_a: "local")  # sin client ni cuota
    monkeypatch.setattr(daemon, "ask_local", lambda t: textos.append(t) or "listo")
    monkeypatch.setattr(
        daemon.ears, "hotkey_solicitada", lambda: respuestas.pop(0) if respuestas else False
    )
    guion = _Guion(gatillos, sesion)
    monkeypatch.setattr(daemon, "_esperar_turno", guion.esperar_turno)
    monkeypatch.setattr(daemon, "_escuchar_en_sesion", guion.escuchar_en_sesion)
    asyncio.run(daemon.run_daemon(lambda **_k: None))
    return guion, textos


def test_sesion_abierta_reabre_el_microfono_sin_pedir_f2(monkeypatch):
    guion, _ = _correr_daemon(monkeypatch, gatillos=["hola"], sesion=["salir"])
    assert guion.orden == ["gatillo", "sesion"]  # el segundo turno no esperó ninguna pulsación


def test_sesion_abierta_encadena_varios_turnos(monkeypatch):
    guion, textos = _correr_daemon(
        monkeypatch, gatillos=["hola"], sesion=["y la hora", "y el clima", "salir"]
    )
    assert guion.orden == ["gatillo", "sesion", "sesion", "sesion"]
    assert textos == ["hola", "y la hora", "y el clima"]


def test_f2_con_el_microfono_abierto_cierra_la_sesion(monkeypatch):
    # la captura devuelve None (toque o silencio largo) -> se vuelve a esperar el gatillo
    guion, _ = _correr_daemon(monkeypatch, gatillos=["hola", "salir"], sesion=[None])
    assert guion.orden == ["gatillo", "sesion", "gatillo"]


def test_sin_open_session_cada_turno_vuelve_a_pedir_f2(monkeypatch):
    """Con la bandera apagada, el flujo previo queda exacto: una pulsación por turno."""
    guion, _ = _correr_daemon(
        monkeypatch, gatillos=["hola", "salir"], sesion=[], open_session=False
    )
    assert guion.orden == ["gatillo", "gatillo"]


def test_f2_durante_la_transcripcion_no_cierra_la_sesion_y_fusiona(monkeypatch):
    """El caso que pidió el usuario: cortar, seguir hablando y que todo llegue junto al modelo."""
    guion, textos = _correr_daemon(
        monkeypatch,
        gatillos=["dame el resumen del"],
        sesion=["repo completo", "salir"],
        pide_turno=[True],  # F2 pulsada mientras se transcribía el primer turno
    )
    assert guion.orden == ["gatillo", "sesion", "sesion"]
    assert textos == ["dame el resumen del. repo completo"]


def test_f2_al_escuchar_descarta_el_turno_y_no_abre_sesion(monkeypatch):
    """Fase 🔴: un toque con el micrófono abierto es "déjalo" — ni se procesa ni queda sesión."""
    guion, textos = _correr_daemon(monkeypatch, gatillos=[ears.CANCELADO, "salir"], sesion=[])
    assert guion.orden == ["gatillo", "gatillo"]  # la segunda vuelta volvió a esperar F2
    assert textos == []


def test_cancelar_descarta_lo_que_se_venia_arrastrando(monkeypatch):
    """Cerrar es cerrar: lo cortado antes no puede reaparecer pegado al turno siguiente."""
    guion, textos = _correr_daemon(
        monkeypatch,
        gatillos=["dame el resumen", ears.CANCELADO, "salir"],
        sesion=[],
        open_session=False,
        pide_turno=[True],  # F2 durante la transcripción del primero -> queda pendiente
    )
    assert guion.orden == ["gatillo", "gatillo", "gatillo"]
    assert textos == []  # "salir" llegó limpio (si no, el fusionado no habría sido el comando)


def test_barge_in_al_hablar_arrastra_lo_pedido_y_mantiene_la_sesion(monkeypatch):
    """Fases 🟣/🟢: cortar el habla no tira el turno cortado, lo fusiona con lo que sigue."""
    guion, textos = _correr_daemon(
        monkeypatch,
        gatillos=["dame el resumen"],
        sesion=["del repo completo", "salir"],
        cortes=[True],  # F2 mientras Escapement decía la primera respuesta
    )
    assert guion.orden == ["gatillo", "sesion", "sesion"]  # la sesión sigue abierta
    assert textos == ["dame el resumen", "dame el resumen. del repo completo"]


def test_habla_completa_no_arrastra_nada(monkeypatch):
    """Sin corte, cada turno viaja solo: el arrastre es exclusivo del barge-in."""
    _, textos = _correr_daemon(
        monkeypatch, gatillos=["dame el resumen"], sesion=["y la hora", "salir"]
    )
    assert textos == ["dame el resumen", "y la hora"]


def test_pausar_la_escucha_cierra_la_sesion(monkeypatch):
    """Pausar desde el tray es dejar de escuchar: el micrófono abierto no puede sobrevivirlo."""
    guion, textos = _correr_daemon(
        monkeypatch, gatillos=["hola", "salir"], sesion=[], pausas=[True]
    )
    assert guion.orden == ["gatillo", "gatillo"]
    assert textos == []  # el turno pausado no llegó a procesarse


# ---------------------------------------------------------------- arrastre de contexto (_fusionar)


def test_fusionar_separa_con_punto_si_el_previo_no_cerraba():
    assert daemon._fusionar("dame el resumen del", "mejor del repo completo") == (
        "dame el resumen del. mejor del repo completo"
    )


def test_fusionar_respeta_la_puntuacion_del_previo():
    assert daemon._fusionar("olvida eso.", "mejor dime la hora") == "olvida eso. mejor dime la hora"
    assert daemon._fusionar("espera,", "mejor no") == "espera, mejor no"


def test_fusionar_sin_previo_devuelve_lo_actual():
    assert daemon._fusionar("", "hola") == "hola"
    assert daemon._fusionar("   ", "hola") == "hola"


def test_fusionar_sin_actual_conserva_lo_arrastrado():
    """Un turno cortado que no transcribió nada no puede borrar lo que ya se venía arrastrando."""
    assert daemon._fusionar("lo de antes", "") == "lo de antes"


# ------------------------------------------------- prioridad de F2 sobre el turno (_consumir_...)


class _ClientLento:
    """Client cuya respuesta nunca llega sola; solo `interrupt()` la corta."""

    def __init__(self):
        self.interrumpido = False
        self._fin = asyncio.Event()

    async def receive_response(self):
        await self._fin.wait()
        return
        yield  # pragma: no cover — solo para que sea async generator

    async def interrupt(self):
        self.interrumpido = True
        self._fin.set()


def _interrumpible(client, cola=None):
    async def go():
        return await daemon._consumir_interrumpible(client, cola)

    return asyncio.run(go())


def test_consumir_interrumpible_sin_barge_in_no_vigila(monkeypatch):
    from claude_agent_sdk import AssistantMessage, TextBlock

    monkeypatch.setattr(config, "VOICE_BARGE_IN", False)
    LATCH.press()  # aunque el usuario pulse, sin barge-in el turno se escucha entero
    msgs = [AssistantMessage(content=[TextBlock(text="hola")], model="m")]
    assert _interrumpible(_ClientFalso(msgs)) == ("hola", None, False)


def test_consumir_interrumpible_termina_solo_sin_marcar_interrupcion(monkeypatch):
    from claude_agent_sdk import AssistantMessage, TextBlock

    monkeypatch.setattr(config, "VOICE_BARGE_IN", True)
    msgs = [AssistantMessage(content=[TextBlock(text="hola")], model="m")]
    assert _interrumpible(_ClientFalso(msgs)) == ("hola", None, False)


def test_consumir_interrumpible_corta_el_turno_con_la_hotkey(monkeypatch):
    monkeypatch.setattr(config, "VOICE_BARGE_IN", True)
    client = _ClientLento()
    LATCH.press()  # F2 pulsada mientras el modelo respondía
    reply, sid, interrumpido = _interrumpible(client)
    assert interrumpido is True
    assert client.interrumpido is True
    assert (reply, sid) == ("", None)


def test_consumir_interrumpible_aborta_la_cola_de_habla(monkeypatch):
    """Callar es lo urgente: la cola se cancela, no se cierra esperando lo encolado."""
    monkeypatch.setattr(config, "VOICE_BARGE_IN", True)
    cancelada = []
    cola = daemon.speech.SpeechQueue(lambda _f: False)
    cola.cancel = lambda timeout=None: cancelada.append(timeout)
    LATCH.press()
    _interrumpible(_ClientLento(), cola)
    assert cancelada == [daemon._TTS_CLOSE_TIMEOUT]


def test_barge_in_tambien_pide_parar_la_corrida_del_plan(monkeypatch):
    """Cortar el turno no basta: `ejecutar` corre en un hilo que sobrevive al `interrupt()`."""
    monkeypatch.setattr(config, "VOICE_BARGE_IN", True)
    LATCH.press()
    _interrumpible(_ClientLento())

    assert RUN.pedido() is True
    assert config.VOICE_HOTKEY.upper() in RUN.motivo()  # el reporte dice POR QUÉ se canceló


def test_turno_que_termina_solo_no_pide_cancelar_nada(monkeypatch):
    """Sin F2 no hay cancelación: si no, un turno normal mataría la corrida que él mismo lanzó."""
    from claude_agent_sdk import AssistantMessage, TextBlock

    monkeypatch.setattr(config, "VOICE_BARGE_IN", True)
    msgs = [AssistantMessage(content=[TextBlock(text="hola")], model="m")]
    _interrumpible(_ClientFalso(msgs))

    assert RUN.pedido() is False


def test_sin_barge_in_no_se_cancela_la_corrida(monkeypatch):
    monkeypatch.setattr(config, "VOICE_BARGE_IN", False)
    LATCH.press()
    from claude_agent_sdk import AssistantMessage, TextBlock

    _interrumpible(_ClientFalso([AssistantMessage(content=[TextBlock(text="ok")], model="m")]))

    assert RUN.pedido() is False


def test_consumir_interrumpible_no_deja_tareas_vivas(monkeypatch):
    """La guardia de la hotkey debe morir con el turno, no quedar sondeando para siempre."""
    from claude_agent_sdk import AssistantMessage, TextBlock

    monkeypatch.setattr(config, "VOICE_BARGE_IN", True)
    pendientes = []

    async def go():
        msgs = [AssistantMessage(content=[TextBlock(text="hola")], model="m")]
        await daemon._consumir_interrumpible(_ClientFalso(msgs), None)
        await asyncio.sleep(0)
        pendientes.extend(t for t in asyncio.all_tasks() if t is not asyncio.current_task())

    asyncio.run(go())
    assert pendientes == []
