"""Tests de la señal de cancelación cooperativa (`agent.cancel`). Sin hilos pesados ni I/O."""

import threading

import pytest

from agent.cancel import RUN, CancelSignal


@pytest.fixture(autouse=True)
def _run_limpia():
    """`RUN` es un singleton de proceso: dejarlo pedido contaminaría al resto de la suite."""
    RUN.limpiar()
    yield
    RUN.limpiar()


def test_arranca_sin_pedir_nada():
    s = CancelSignal()
    assert s.pedido() is False
    assert s.motivo() == ""


def test_pedir_levanta_la_bandera_con_motivo():
    s = CancelSignal()
    s.pedir("cortaste el turno con F2")

    assert s.pedido() is True
    assert s.motivo() == "cortaste el turno con F2"


def test_pedir_sin_motivo_deja_la_bandera_igual():
    """El motivo es para el reporte; que falte no puede impedir la cancelación."""
    s = CancelSignal()
    s.pedir()

    assert s.pedido() is True
    assert s.motivo() == ""


def test_limpiar_borra_bandera_y_motivo():
    s = CancelSignal()
    s.pedir("algo")
    s.limpiar()

    assert s.pedido() is False
    assert s.motivo() == ""  # si sobreviviera, el reporte de la corrida SIGUIENTE mentiría


def test_pedir_dos_veces_se_queda_con_el_ultimo_motivo():
    s = CancelSignal()
    s.pedir("primero")
    s.pedir("segundo")

    assert s.motivo() == "segundo"


def test_pedido_desde_otro_hilo_lo_ve_el_lector():
    """El caso real: el daemon pide desde el hilo del loop y el runner lee desde el suyo."""
    s = CancelSignal()
    visto = threading.Event()

    def _lector():
        while not s.pedido():
            pass
        visto.set()

    hilo = threading.Thread(target=_lector, daemon=True)
    hilo.start()
    s.pedir("desde el hilo principal")

    assert visto.wait(timeout=2.0) is True
    hilo.join(timeout=2.0)
    assert s.motivo() == "desde el hilo principal"


def test_run_es_una_señal_compartida_del_proceso():
    """Hay una sola porque `_RUN_LOCK` ya garantiza una corrida a la vez."""
    assert isinstance(RUN, CancelSignal)
    RUN.pedir("prueba")
    assert RUN.pedido() is True
