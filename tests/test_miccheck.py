"""Tests del diagnóstico de micrófono: el análisis es puro, no toca audio ni STT."""

import pytest

from agent.voice.ears import _CALIB_FRAMES, _RMS_CEIL, _RMS_FLOOR
from agent.voice.miccheck import analizar, _barra


def _serie(piso: float, voz: float, frames_voz: int = 20) -> list[float]:
    """Calibración a ``piso`` seguida de ``frames_voz`` frames a ``voz``."""
    return [piso] * _CALIB_FRAMES + [voz] * frames_voz


def test_muy_pocos_frames_no_calibra_y_no_hay_voz():
    """Sin los ~300 ms de calibración el umbral queda en el valor exigente inicial."""
    diag = analizar([0.2] * (_CALIB_FRAMES - 1))

    assert diag.umbral == _RMS_CEIL
    assert diag.hubo_voz is False
    assert diag.pico == 0.2  # el pico se reporta igual: sirve para distinguir mudo de bajo


def test_serie_vacia_no_revienta():
    diag = analizar([])

    assert diag.pico == 0.0
    assert diag.frames == 0


def test_silencio_digital_da_veredicto_mudo():
    diag = analizar(_serie(0.0, 0.0))

    assert diag.pico == 0.0
    assert diag.hubo_voz is False
    assert diag.veredicto.startswith("MUDO")


def test_senal_por_debajo_del_umbral_da_veredicto_bajo():
    """El caso real: el mic capta, pero tan bajo que la captura descarta el turno."""
    diag = analizar(_serie(0.0005, 0.006))  # 0.006 < _RMS_FLOOR (0.012)

    assert diag.hubo_voz is False
    assert diag.veredicto.startswith("BAJO")
    assert diag.margen_db < 0


def test_voz_clara_da_veredicto_ok():
    diag = analizar(_serie(0.001, 0.1, frames_voz=15))

    assert diag.hubo_voz is True
    assert diag.frames_con_voz == 15
    assert diag.frames == 15
    assert diag.veredicto.startswith("OK")


def test_voz_apenas_sobre_el_umbral_avisa_que_esta_justo():
    diag = analizar(_serie(0.001, _RMS_FLOOR * 1.2))  # +1.6 dB: pasa, pero raspando

    assert diag.hubo_voz is True
    assert diag.veredicto.startswith("JUSTO")


def test_la_calibracion_no_cuenta_como_voz():
    """Un golpe fuerte al arranque no puede hacer pasar por buena una grabación muda."""
    diag = analizar([0.3] * _CALIB_FRAMES + [0.0] * 20)

    assert diag.frames_con_voz == 0
    assert diag.hubo_voz is False


def test_umbral_acotado_por_abajo_con_piso_silencioso():
    diag = analizar(_serie(0.0, 0.1))

    assert diag.umbral == _RMS_FLOOR  # 3*0 = 0 -> se aplica el mínimo absoluto


def test_umbral_acotado_por_arriba_con_mucho_ruido_de_fondo():
    diag = analizar(_serie(0.5, 0.6))

    assert diag.umbral == _RMS_CEIL  # 3*0.5 = 1.5 -> se aplica el tope


def test_umbral_sigue_al_piso_de_ruido_en_el_rango_medio():
    diag = analizar(_serie(0.01, 0.1))

    assert diag.umbral == pytest.approx(0.03)


def test_margen_db_es_cero_justo_en_el_umbral():
    diag = analizar(_serie(0.0, _RMS_FLOOR))

    assert diag.margen_db == pytest.approx(0.0)


def test_margen_db_de_una_grabacion_muda_es_menos_infinito():
    assert analizar(_serie(0.0, 0.0)).margen_db == float("-inf")


def test_barra_marca_la_posicion_del_umbral():
    assert "|" in _barra(0.0, 0.05, ancho=40)


def test_barra_llena_no_se_pasa_de_ancho():
    barra = _barra(10.0, 0.012, ancho=40)

    assert len(barra) == 40
    assert barra == "█" * 40  # con la barra saturada la marca queda tapada, no desborda
