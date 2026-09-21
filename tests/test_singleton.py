"""Tests del lock de instancia única (P3: el daemon de voz no se duplica)."""

from agent import singleton


def test_segunda_instancia_no_obtiene_el_lock():
    port = 49911
    assert singleton.acquire_single_instance(port) is True
    assert singleton.acquire_single_instance(port) is False  # ya ocupado -> no


def test_puertos_distintos_no_colisionan():
    assert singleton.acquire_single_instance(49912) is True
    assert singleton.acquire_single_instance(49913) is True


def test_release_permite_readquirir():
    port = 49915
    assert singleton.acquire_single_instance(port) is True
    singleton.release_single_instance()  # suelta el lock (reinicio limpio)
    assert singleton.acquire_single_instance(port) is True  # tras liberar, se retoma
