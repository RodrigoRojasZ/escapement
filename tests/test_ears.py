"""Tests de ears: hotkey_pressed (barge-in) y captura manos libres. Sin hardware real."""

import sys
import types

import numpy as np
import pytest

from agent import bus, config
from agent.voice import ears, stt
from agent.voice.hotkey import LATCH


@pytest.fixture(autouse=True)
def _estado_limpio(monkeypatch):
    """El latch y la fase son proceso-globales: ningún test debe heredarlos del anterior.

    Además neutraliza el prewarm del STT: abrir el micrófono lo dispara, y aquí llegaría a
    descargar y cargar faster-whisper de verdad. Los tests que lo miran instalan su propio doble.
    """
    monkeypatch.setattr(stt, "prewarm", lambda: False)
    LATCH.stop()
    ears._reset_fase()
    yield
    LATCH.stop()
    ears._reset_fase()


def _fake_keyboard(monkeypatch, is_pressed):
    monkeypatch.setitem(sys.modules, "keyboard", types.SimpleNamespace(is_pressed=is_pressed))


def test_hotkey_presionada_usa_la_de_config(monkeypatch):
    vistos = []

    def is_pressed(key):
        vistos.append(key)
        return True

    _fake_keyboard(monkeypatch, is_pressed)
    assert ears.hotkey_pressed() is True
    assert vistos == [config.VOICE_HOTKEY]


def test_hotkey_no_presionada(monkeypatch):
    _fake_keyboard(monkeypatch, lambda _k: False)
    assert ears.hotkey_pressed() is False


def test_hotkey_explicita_tiene_prioridad(monkeypatch):
    vistos = []

    def is_pressed(key):
        vistos.append(key)
        return False

    _fake_keyboard(monkeypatch, is_pressed)
    ears.hotkey_pressed("f9")
    assert vistos == ["f9"]


def test_hook_roto_devuelve_false_sin_lanzar(monkeypatch):
    def explota(_k):
        raise OSError("hook de teclado no disponible (UAC/RDP)")

    _fake_keyboard(monkeypatch, explota)
    assert ears.hotkey_pressed() is False


# ------------------------------------------------- manos libres (record_until_silence)

_FRAME = int(ears.SAMPLE_RATE * 0.03)  # 480 muestras = 30 ms, el frame de record_until_silence


class _Reloj:
    """time falso: monotonic() controlable, avanzado por los read() del stream falso."""

    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


class _StreamFake:
    """InputStream falso: entrega frames guionados; agotados, silencio. Cada read = +30 ms."""

    def __init__(self, reloj, frames):
        self._reloj, self._frames = reloj, list(frames)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self, n):
        self._reloj.t += 0.03
        if self._frames:
            return self._frames.pop(0).reshape(-1, 1), False
        return np.zeros((n, 1), dtype="float32"), False


def _con_captura(monkeypatch, frames):
    reloj = _Reloj()
    monkeypatch.setattr(ears, "time", reloj)
    fake_sd = types.SimpleNamespace(InputStream=lambda **_kw: _StreamFake(reloj, frames))
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)


def _frames(n, amplitud):
    return [np.full(_FRAME, amplitud, dtype="float32") for _ in range(n)]


def test_record_until_silence_corta_tras_el_silencio(monkeypatch):
    # 10 frames de calibración (ruido bajo) + voz + silencio: corta a los 1.2 s de silencio.
    frames = _frames(10, 0.001) + _frames(5, 0.1) + _frames(60, 0.001)
    _con_captura(monkeypatch, frames)
    audio = ears.record_until_silence()
    assert audio is not None
    # 10 calibración + 5 voz + 40 de silencio (1.2 s / 30 ms), incluyendo el contexto previo
    assert len(audio) == (10 + 5 + 40) * _FRAME


def test_record_until_silence_sin_voz_devuelve_none(monkeypatch):
    _con_captura(monkeypatch, [])  # solo silencio -> _WAKE_START_TIMEOUT_S y aborta
    assert ears.record_until_silence() is None


def test_record_until_silence_respeta_tope_maximo(monkeypatch):
    _con_captura(monkeypatch, _frames(2000, 0.1))  # habla sin parar -> corta en _WAKE_MAX_S
    audio = ears.record_until_silence()
    assert audio is not None
    assert len(audio) <= int(ears._WAKE_MAX_S / 0.03 + 1) * _FRAME


def test_record_until_silence_umbral_se_adapta_al_ruido(monkeypatch):
    # Piso de ruido 0.01 -> umbral 3x = 0.03: una "voz" de 0.02 no dispara la grabación.
    frames = _frames(10, 0.01) + _frames(20, 0.02)
    _con_captura(monkeypatch, frames)
    assert ears.record_until_silence() is None


# ------------------------------------------------- toggle (record_toggle)


def _con_toggle(monkeypatch, frames, presionada, hooks=None):
    """Monta reloj + stream + keyboard falsos para record_toggle.

    ``presionada``: intervalos [(ini, fin), ...] del reloj falso en que is_pressed es True.
    ``hooks``: lista donde registrar los ``on_press_key`` enganchados (el callback nunca
    dispara solo; los tests simulan la pulsación con ``LATCH.press()``).

    Cada tecla presionada marca además el latch, como haría el hook real del proceso: así los
    tests ven las mismas pulsaciones pendientes que vería el daemon.
    """
    reloj = _Reloj()
    monkeypatch.setattr(ears, "time", reloj)
    fake_sd = types.SimpleNamespace(InputStream=lambda **_kw: _StreamFake(reloj, frames))
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)

    def _on_press_key(k, cb):
        if hooks is not None:
            hooks.append((k, cb))
        return object()

    def _is_pressed(_k):
        if not any(a <= reloj.t < b for a, b in presionada):
            return False
        LATCH.press()
        return True

    fake_kb = types.SimpleNamespace(
        is_pressed=_is_pressed,
        wait=lambda _k: None,
        on_press_key=_on_press_key,
        unhook=lambda _h: None,
    )
    monkeypatch.setitem(sys.modules, "keyboard", fake_kb)
    return reloj


def test_record_toggle_corta_por_silencio(monkeypatch):
    # activación en t=0 (tecla ya presionada) + calibración + voz + silencio: corta a los 2 s.
    frames = _frames(10, 0.001) + _frames(5, 0.1) + _frames(200, 0.001)
    _con_toggle(monkeypatch, frames, presionada=[(0.0, 0.06)])
    audio = ears.record_toggle()
    assert audio is not None
    # 10 calibración + 5 voz + 67 de silencio (2.0 s / 30 ms, redondeado hacia arriba)
    assert len(audio) == (10 + 5 + 67) * _FRAME


def test_record_toggle_cancela_con_el_segundo_toque(monkeypatch):
    """Fase 🔴: con el micrófono abierto, F2 descarta el turno (no lo manda a transcribir)."""
    # voz continua (el silencio nunca corta); el segundo toque en t=1.0 cancela.
    frames = _frames(10, 0.001) + _frames(400, 0.1)
    _con_toggle(monkeypatch, frames, presionada=[(0.0, 0.06), (1.0, 1.2)])
    assert ears.record_toggle() is ears.CANCELADO


def test_cancelado_es_un_texto_vacio(monkeypatch):
    """Quien no lo distinga por identidad lo trata como "nada que procesar", no como audio."""
    assert isinstance(ears.CANCELADO, str)
    assert ears.CANCELADO == ""
    assert not ears.CANCELADO


def test_record_toggle_cancelado_deja_la_fase_inactiva(monkeypatch):
    """Sin STT por delante, `transcribiendo` sería mentira: el ícono debe volver a azul."""
    frames = _frames(10, 0.001) + _frames(400, 0.1)
    _con_toggle(monkeypatch, frames, presionada=[(0.0, 0.06), (1.0, 1.2)])
    assert ears.record_toggle() is ears.CANCELADO
    assert ears.fase_actual() == ears.CAPTURA_INACTIVA


def test_record_toggle_sin_voz_devuelve_none(monkeypatch):
    # activó pero nunca habló: cierra sin transcribir al llegar a _TOGGLE_START_TIMEOUT_S
    _con_toggle(monkeypatch, [], presionada=[(0.0, 0.06)])
    assert ears.record_toggle() is None


def test_record_toggle_respeta_tope_maximo(monkeypatch):
    _con_toggle(monkeypatch, _frames(10, 0.001) + _frames(3200, 0.1), presionada=[(0.0, 0.06)])
    audio = ears.record_toggle()
    assert audio is not None
    assert len(audio) <= int(ears._TOGGLE_MAX_S / 0.03 + 1) * _FRAME


def test_record_toggle_espera_la_pulsacion_con_la_hotkey_de_config(monkeypatch):
    # tecla libre al entrar -> engancha el latch (hook persistente) con la hotkey de config
    hooks: list[tuple] = []
    _con_toggle(monkeypatch, [], presionada=[], hooks=hooks)
    assert ears.record_toggle(wait_timeout=0.01) is ears._TIMEOUT
    assert [k for k, _cb in hooks] == [config.VOICE_HOTKEY]


def test_record_toggle_consume_una_pulsacion_previa_sin_esperar(monkeypatch):
    """F2 pulsada mientras se transcribía abre el turno siguiente sin exigir otra pulsación."""
    hooks: list[tuple] = []
    _con_toggle(monkeypatch, [], presionada=[], hooks=hooks)
    LATCH.press()  # el usuario pulsó con el micrófono cerrado
    assert ears.record_toggle(wait_timeout=0.01) is None  # grabó (sin voz -> None), no expiró
    assert hooks == []  # ni esperó ni tuvo que enganchar nada: el latch ya lo sabía
    assert not LATCH.pending()  # consumida: no reabre también el turno de después


def test_record_toggle_timeout_de_espera_devuelve_sentinel(monkeypatch):
    # con wait_timeout y sin pulsación, devuelve _TIMEOUT (el daemon congela con esto)
    _con_toggle(monkeypatch, [], presionada=[])
    assert ears.record_toggle(wait_timeout=0.01) is ears._TIMEOUT


def test_record_toggle_no_deja_pendiente_el_toque_que_lo_cerro(monkeypatch):
    """El segundo toque cancela el turno; si quedara en el latch, reabriría el micrófono solo."""
    frames = _frames(10, 0.001) + _frames(400, 0.1)
    _con_toggle(monkeypatch, frames, presionada=[(0.0, 0.06), (1.0, 1.2)])
    assert ears.record_toggle() is ears.CANCELADO
    assert not LATCH.pending()


# ------------------------------------------------- sesión abierta (record_sesion)


def test_record_sesion_graba_sin_esperar_pulsacion(monkeypatch):
    # el micrófono ya está abierto: nadie engancha hooks ni espera nada, se graba de una
    hooks: list[tuple] = []
    frames = _frames(10, 0.001) + _frames(5, 0.1) + _frames(200, 0.001)
    _con_toggle(monkeypatch, frames, presionada=[], hooks=hooks)
    audio = ears.record_sesion()
    assert audio is not None
    # 10 calibración + 5 voz + 54 de silencio (1.6 s / 30 ms, redondeado hacia arriba)
    assert len(audio) == (10 + 5 + 54) * _FRAME
    assert hooks == []


def test_record_sesion_se_cierra_con_un_toque(monkeypatch):
    # voz continua (el silencio nunca corta); el toque en t=1.0 cierra la SESIÓN, no el turno
    frames = _frames(10, 0.001) + _frames(400, 0.1)
    _con_toggle(monkeypatch, frames, presionada=[(1.0, 1.2)])
    assert ears.record_sesion() is None


def test_record_sesion_ignora_la_tecla_heredada_del_barge_in(monkeypatch):
    """El toque que cortó la respuesta anterior pide hablar, no cerrar la sesión."""
    frames = _frames(10, 0.001) + _frames(5, 0.1) + _frames(200, 0.001)
    _con_toggle(monkeypatch, frames, presionada=[(0.0, 0.06)])  # F2 todavía puesta al entrar
    audio = ears.record_sesion()
    assert audio is not None  # grabó el turno entero en vez de cerrar en el primer frame
    assert len(audio) == (10 + 5 + 54) * _FRAME
    assert not LATCH.pending()  # la pulsación heredada no reabre nada después


def test_record_sesion_se_cierra_si_nadie_habla(monkeypatch):
    _con_toggle(monkeypatch, [], presionada=[])  # solo silencio -> _SESION_ESPERA_S
    assert ears.record_sesion() is None


def test_record_sesion_respeta_tope_maximo(monkeypatch):
    _con_toggle(monkeypatch, _frames(10, 0.001) + _frames(3200, 0.1), presionada=[])
    audio = ears.record_sesion()
    assert audio is not None
    assert len(audio) <= int(ears._SESION_MAX_S / 0.03 + 1) * _FRAME


def test_record_sesion_no_deja_pendiente_el_toque_que_la_cerro(monkeypatch):
    """Si el toque de cierre quedara en el latch, el daemon reabriría el micrófono al instante."""
    frames = _frames(10, 0.001) + _frames(400, 0.1)
    _con_toggle(monkeypatch, frames, presionada=[(1.0, 1.2)])
    assert ears.record_sesion() is None
    assert not LATCH.pending()


def test_record_sesion_con_voz_encadena_a_transcribiendo(monkeypatch, fases):
    frames = _frames(10, 0.001) + _frames(5, 0.1) + _frames(200, 0.001)
    _con_toggle(monkeypatch, frames, presionada=[])
    assert ears.record_sesion() is not None
    assert fases == [ears.CAPTURA_ESCUCHANDO, ears.CAPTURA_TRANSCRIBIENDO]


def test_record_sesion_al_cerrar_vuelve_a_inactivo(monkeypatch, fases):
    """Aunque se haya hablado: cerrar descarta el audio, no hay nada que transcribir."""
    frames = _frames(10, 0.001) + _frames(400, 0.1)
    _con_toggle(monkeypatch, frames, presionada=[(1.0, 1.2)])
    assert ears.record_sesion() is None
    assert fases == [ears.CAPTURA_ESCUCHANDO, ears.CAPTURA_INACTIVA]


def test_record_sesion_precalienta_el_stt(monkeypatch, prewarms):
    _con_toggle(monkeypatch, [], presionada=[])
    assert ears.record_sesion() is None
    assert len(prewarms) == 1


def test_listen_once_en_toggle_usa_record_toggle(monkeypatch):
    monkeypatch.setattr(ears.config, "VOICE_PTT_MODE", "toggle")
    llamadas = []
    monkeypatch.setattr(
        ears,
        "record_toggle",
        lambda hk=None, wait_timeout=None: llamadas.append(hk) or np.ones(100, dtype="float32"),
    )
    monkeypatch.setitem(
        sys.modules, "soundfile", types.SimpleNamespace(write=lambda *_a, **_k: None)
    )
    monkeypatch.setattr(ears.stt, "transcribe", lambda wav, **kw: "hola escapement")
    assert ears.listen_once() == "hola escapement"
    assert llamadas == [None]


def test_listen_once_en_hold_usa_record_while_held(monkeypatch):
    monkeypatch.setattr(ears.config, "VOICE_PTT_MODE", "hold")
    llamadas = []
    monkeypatch.setattr(
        ears,
        "record_while_held",
        lambda hk=None, wait_timeout=None: llamadas.append(hk) or np.ones(100, dtype="float32"),
    )
    monkeypatch.setattr(
        ears, "record_toggle", lambda *a, **k: (_ for _ in ()).throw(AssertionError)
    )
    monkeypatch.setitem(
        sys.modules, "soundfile", types.SimpleNamespace(write=lambda *_a, **_k: None)
    )
    monkeypatch.setattr(ears.stt, "transcribe", lambda wav, **kw: "modo clasico")
    assert ears.listen_once() == "modo clasico"
    assert llamadas == [None]


def test_listen_hands_free_sin_voz_no_transcribe(monkeypatch):
    monkeypatch.setattr(ears, "record_until_silence", lambda: None)
    monkeypatch.setattr(
        ears.stt, "transcribe", lambda *a, **k: (_ for _ in ()).throw(AssertionError)
    )
    monkeypatch.setitem(sys.modules, "soundfile", types.SimpleNamespace())
    assert ears.listen_hands_free() == ""


def test_listen_hands_free_transcribe_con_vad(monkeypatch):
    escritos, llamadas = [], []
    monkeypatch.setattr(ears, "record_until_silence", lambda: np.ones(100, dtype="float32"))
    monkeypatch.setitem(
        sys.modules,
        "soundfile",
        types.SimpleNamespace(write=lambda path, audio, sr: escritos.append((path, sr))),
    )
    monkeypatch.setattr(
        ears.stt, "transcribe", lambda wav, **kw: llamadas.append((wav, kw)) or "dime la hora"
    )
    assert ears.listen_hands_free() == "dime la hora"
    assert escritos[0][1] == ears.SAMPLE_RATE
    wav, kwargs = llamadas[0]
    assert str(wav).endswith("_wake.wav")
    assert kwargs == {"vad_filter": True}


# ------------------------------------------------- fase de captura en el bus (voice.capture)


@pytest.fixture
def fases():
    """Recolecta los ``state`` publicados en ``voice.capture`` durante el test."""
    vistos: list[str] = []
    bus._reset()
    bus.subscribe("voice.capture", lambda ev: vistos.append(ev.data["state"]))
    yield vistos
    bus._reset()


def test_record_toggle_con_voz_encadena_a_transcribiendo(monkeypatch, fases):
    # con audio la etapa siguiente es el STT: publicar `inactivo` aquí haría parpadear el ícono
    frames = _frames(10, 0.001) + _frames(5, 0.1) + _frames(200, 0.001)
    _con_toggle(monkeypatch, frames, presionada=[(0.0, 0.06)])
    assert ears.record_toggle() is not None
    assert fases == [ears.CAPTURA_ESCUCHANDO, ears.CAPTURA_TRANSCRIBIENDO]


def test_record_toggle_sin_voz_vuelve_a_inactivo(monkeypatch, fases):
    # activó y no habló: no hay nada que transcribir, el ciclo cierra
    _con_toggle(monkeypatch, [], presionada=[(0.0, 0.06)])
    assert ears.record_toggle() is None
    assert fases == [ears.CAPTURA_ESCUCHANDO, ears.CAPTURA_INACTIVA]


def test_record_toggle_no_publica_si_expira_la_espera(monkeypatch, fases):
    # nunca se abrió el micrófono: el ícono no debe parpadear en rojo
    _con_toggle(monkeypatch, [], presionada=[])
    assert ears.record_toggle(wait_timeout=0.01) is ears._TIMEOUT
    assert fases == []


def test_record_until_silence_sin_voz_igual_vuelve_a_inactivo(monkeypatch, fases):
    # falso positivo de la wake word: sale por `return None` dentro del try -> el finally corre
    _con_captura(monkeypatch, [])
    assert ears.record_until_silence() is None
    assert fases == [ears.CAPTURA_ESCUCHANDO, ears.CAPTURA_INACTIVA]


# ------------------------------------------------- precalentamiento del STT al abrir el micrófono


@pytest.fixture
def prewarms(monkeypatch):
    """Cuenta las llamadas a ``stt.prewarm`` (el autouse ya lo tiene neutralizado)."""
    llamadas: list[int] = []
    monkeypatch.setattr(stt, "prewarm", lambda: llamadas.append(1) or False)
    return llamadas


def test_record_toggle_precalienta_el_stt_al_abrir_el_microfono(monkeypatch, prewarms):
    # la carga del modelo (~5 s en frío) se solapa con lo que el usuario tarda en hablar
    frames = _frames(10, 0.001) + _frames(5, 0.1) + _frames(200, 0.001)
    _con_toggle(monkeypatch, frames, presionada=[(0.0, 0.06)])
    assert ears.record_toggle() is not None
    assert len(prewarms) == 1


def test_record_toggle_no_precalienta_si_expira_la_espera(monkeypatch, prewarms):
    # nunca se abrió el micrófono: cargar el modelo aquí desharía el congelamiento del daemon
    _con_toggle(monkeypatch, [], presionada=[])
    assert ears.record_toggle(wait_timeout=0.01) is ears._TIMEOUT
    assert prewarms == []


def test_record_until_silence_precalienta_el_stt(monkeypatch, prewarms):
    # manos libres: el turno también termina en STT, así que se precalienta igual
    _con_captura(monkeypatch, [])
    assert ears.record_until_silence() is None
    assert len(prewarms) == 1


def test_record_while_held_publica_aunque_el_stream_falle(monkeypatch, fases):
    # el micrófono se cae a mitad: el ícono no puede quedarse en rojo para siempre
    def _stream_roto(**_kw):
        raise OSError("dispositivo de audio ocupado")

    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(InputStream=_stream_roto))
    monkeypatch.setitem(sys.modules, "keyboard", types.SimpleNamespace(is_pressed=lambda _k: True))
    LATCH.press()  # el turno arranca por una pulsación ya registrada por el hook del proceso
    with pytest.raises(OSError, match="ocupado"):
        ears.record_while_held()
    assert fases == [ears.CAPTURA_ESCUCHANDO, ears.CAPTURA_INACTIVA]


def test_listen_once_publica_transcribiendo_alrededor_del_stt(monkeypatch, fases):
    monkeypatch.setattr(ears.config, "VOICE_PTT_MODE", "toggle")
    monkeypatch.setattr(
        ears, "record_toggle", lambda hk=None, wait_timeout=None: np.ones(100, dtype="float32")
    )
    monkeypatch.setitem(
        sys.modules, "soundfile", types.SimpleNamespace(write=lambda *_a, **_k: None)
    )
    monkeypatch.setattr(ears.stt, "transcribe", lambda wav, **kw: "hola")
    assert ears.listen_once() == "hola"
    # record_toggle está mockeado, así que solo quedan las fases del STT
    assert fases == [ears.CAPTURA_TRANSCRIBIENDO, ears.CAPTURA_INACTIVA]


def test_listen_once_sin_audio_no_publica_transcribiendo(monkeypatch, fases):
    monkeypatch.setattr(ears.config, "VOICE_PTT_MODE", "toggle")
    monkeypatch.setattr(ears, "record_toggle", lambda hk=None, wait_timeout=None: None)
    monkeypatch.setitem(sys.modules, "soundfile", types.SimpleNamespace())
    assert ears.listen_once() == ""
    assert fases == []


def test_listen_once_cancelado_es_no_contestar(monkeypatch, fases):
    """Cancelar con F2 no es `None` (que sería "expiró"): es una respuesta vacía."""
    monkeypatch.setattr(ears.config, "VOICE_PTT_MODE", "toggle")
    monkeypatch.setattr(ears, "record_toggle", lambda hk=None, wait_timeout=None: ears.CANCELADO)
    monkeypatch.setitem(
        sys.modules,
        "soundfile",
        types.SimpleNamespace(
            write=lambda *_a, **_k: pytest.fail("audio descartado, no se transcribe")
        ),
    )
    assert ears.listen_once() == ""
    assert fases == []


# ------------------------------------------------- reserva del micrófono (escucha_exclusiva)


def test_escucha_exclusiva_marca_y_libera():
    assert ears.escucha_reservada() is False
    with ears.escucha_exclusiva():
        assert ears.escucha_reservada() is True
    assert ears.escucha_reservada() is False


def test_escucha_exclusiva_soporta_anidamiento():
    """Solo la reserva más externa libera: una interna no puede destaparle el micrófono."""
    with ears.escucha_exclusiva():
        with ears.escucha_exclusiva():
            assert ears.escucha_reservada() is True
        assert ears.escucha_reservada() is True
    assert ears.escucha_reservada() is False


def test_escucha_exclusiva_libera_ante_excepcion():
    """Si no liberara, el barge-in quedaría muerto para el resto de la vida del proceso."""
    with pytest.raises(RuntimeError):
        with ears.escucha_exclusiva():
            raise RuntimeError("micrófono ocupado")
    assert ears.escucha_reservada() is False
