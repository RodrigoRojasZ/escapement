"""Tests del precalentamiento del STT: nunca se carga el modelo dos veces ni se bloquea al caller.

Sin faster-whisper ni GPU: ``stt._model`` se reemplaza por un ``lru_cache`` falso que cuenta
cargas y puede bloquearse a voluntad, así que la semántica de caché/limpieza es la real.
"""

import threading
from functools import lru_cache

import pytest

from agent.voice import stt


class _Cargador:
    """Doble de ``stt._model``: cuenta cargas y opcionalmente se bloquea dentro de una."""

    def __init__(self, puerta: threading.Event | None = None, explota: bool = False):
        self.cargas = 0
        self.entro = threading.Event()  # se activa al ENTRAR a la carga, antes de bloquearse
        self._puerta = puerta
        self._explota = explota
        self._fn = lru_cache(maxsize=1)(self._cargar)

    def _cargar(self):
        self.cargas += 1
        self.entro.set()
        if self._puerta is not None:
            self._puerta.wait(timeout=5)
        if self._explota:
            raise RuntimeError("CUDA se cayó")
        return f"modelo-{self.cargas}"

    def __call__(self):
        return self._fn()

    def cache_info(self):
        return self._fn.cache_info()

    def cache_clear(self):
        return self._fn.cache_clear()


@pytest.fixture(autouse=True)
def _sin_hilo_heredado():
    """El handle del prewarm es global del módulo: ningún test debe heredar el del anterior."""
    stt._hilo = None
    yield
    hilo = stt._hilo
    if hilo is not None:
        hilo.join(timeout=5)
    stt._hilo = None


def _instalar(monkeypatch, cargador: _Cargador) -> _Cargador:
    monkeypatch.setattr(stt, "_model", cargador)
    return cargador


def _esperar_prewarm() -> None:
    if stt._hilo is not None:
        stt._hilo.join(timeout=5)


def test_prewarm_carga_el_modelo_en_segundo_plano(monkeypatch):
    cargador = _instalar(monkeypatch, _Cargador())

    assert stt.prewarm() is True  # arrancó una carga
    _esperar_prewarm()

    assert cargador.cargas == 1
    assert stt.cargado() is True


def test_prewarm_no_bloquea_al_que_llama(monkeypatch):
    """El punto del prewarm: la captura sigue grabando mientras el modelo carga."""
    puerta = threading.Event()
    cargador = _instalar(monkeypatch, _Cargador(puerta=puerta))

    assert stt.prewarm() is True
    assert cargador.entro.wait(timeout=5)  # el hilo ya está adentro de la carga...
    assert stt.cargado() is False  # ...y aún no terminó, pero prewarm() ya volvió

    puerta.set()
    _esperar_prewarm()
    assert stt.cargado() is True


def test_prewarm_con_modelo_ya_cargado_es_noop(monkeypatch):
    cargador = _instalar(monkeypatch, _Cargador())
    stt._cargar()

    assert stt.prewarm() is False
    assert cargador.cargas == 1


def test_prewarm_repetido_durante_la_carga_no_lanza_un_segundo_hilo(monkeypatch):
    """F2 dos veces seguidas no puede poner dos copias del modelo en la VRAM."""
    puerta = threading.Event()
    cargador = _instalar(monkeypatch, _Cargador(puerta=puerta))

    assert stt.prewarm() is True
    assert cargador.entro.wait(timeout=5)
    assert stt.prewarm() is False  # ya hay una carga en curso

    puerta.set()
    _esperar_prewarm()
    assert cargador.cargas == 1


def test_transcribir_durante_el_prewarm_no_duplica_la_carga(monkeypatch):
    """El caso que ``lru_cache`` sola NO cubre: dos hilos entrando a la vez la ejecutan los dos."""
    puerta = threading.Event()
    cargador = _instalar(monkeypatch, _Cargador(puerta=puerta))

    stt.prewarm()
    assert cargador.entro.wait(timeout=5)

    obtenido: list = []
    turno = threading.Thread(target=lambda: obtenido.append(stt._cargar()))
    turno.start()
    try:
        puerta.set()
        turno.join(timeout=5)
    finally:
        _esperar_prewarm()

    assert cargador.cargas == 1
    assert obtenido == ["modelo-1"]  # el turno reusó el modelo del prewarm, no cargó otro


def test_prewarm_se_traga_el_error_y_el_turno_lo_reintenta(monkeypatch):
    """Si CUDA falla en el prewarm, el hilo muere en silencio: el error debe verse en el turno."""
    cargador = _instalar(monkeypatch, _Cargador(explota=True))

    assert stt.prewarm() is True
    _esperar_prewarm()  # no propaga nada al llamador

    assert stt.cargado() is False
    with pytest.raises(RuntimeError):
        stt._cargar()
    assert cargador.cargas == 2  # el turno reintentó de verdad


def test_cargado_no_dispara_la_carga(monkeypatch):
    cargador = _instalar(monkeypatch, _Cargador())

    assert stt.cargado() is False
    assert cargador.cargas == 0


def test_unload_espera_al_prewarm_en_curso(monkeypatch):
    """Sin el lock, el hilo repoblaría la caché justo después del cache_clear."""
    puerta = threading.Event()
    _instalar(monkeypatch, _Cargador(puerta=puerta))

    stt.prewarm()
    assert stt._hilo is not None and stt._hilo.is_alive()

    descargado = threading.Event()
    threading.Thread(target=lambda: (stt.unload(), descargado.set()), daemon=True).start()
    assert not descargado.wait(timeout=0.2)  # bloqueado esperando a que la carga termine

    puerta.set()
    assert descargado.wait(timeout=5)
    _esperar_prewarm()
    assert stt.cargado() is False  # el congelamiento sí liberó el modelo
