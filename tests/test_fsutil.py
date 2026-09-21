"""Tests de la escritura atómica (F3): sin dejar archivos a medias ni temporales huérfanos."""

import threading

import pytest

from agent import fsutil


def test_write_text_atomic_escribe_contenido(tmp_path):
    dest = tmp_path / "sub" / "state.json"
    fsutil.write_text_atomic(dest, '{"a": 1}')
    assert dest.read_text(encoding="utf-8") == '{"a": 1}'  # crea el dir padre y escribe


def test_write_text_atomic_reemplaza_intacto(tmp_path):
    dest = tmp_path / "plan.json"
    fsutil.write_text_atomic(dest, "viejo")
    fsutil.write_text_atomic(dest, "nuevo")
    assert dest.read_text(encoding="utf-8") == "nuevo"


def test_write_text_atomic_no_deja_temporal(tmp_path):
    dest = tmp_path / "plan.json"
    fsutil.write_text_atomic(dest, "contenido")
    # tras un write exitoso no queda ningún .tmp en el directorio.
    assert [p.name for p in tmp_path.iterdir()] == ["plan.json"]


def test_write_text_atomic_conserva_viejo_si_falla(tmp_path, monkeypatch):
    dest = tmp_path / "plan.json"
    fsutil.write_text_atomic(dest, "intacto")

    def boom(*a, **k):
        raise OSError("disco lleno")

    monkeypatch.setattr(fsutil.os, "replace", boom)
    try:
        fsutil.write_text_atomic(dest, "a-medias")
    except OSError:
        pass
    assert dest.read_text(encoding="utf-8") == "intacto"  # el viejo queda íntegro
    assert [p.name for p in tmp_path.iterdir()] == ["plan.json"]  # el temporal se limpió


# --- S4: lock inter-proceso (serializa el read-modify-write de state.json, H8) ---


def test_locked_adquiere_y_libera(tmp_path):
    dest = tmp_path / "state.json"
    with fsutil.locked(dest):
        pass
    with fsutil.locked(dest, timeout=0.5):  # re-adquirible: el primero liberó de verdad
        pass


def test_locked_excluye_a_un_segundo_tomador(tmp_path):
    dest = tmp_path / "state.json"
    resultado: list[object] = []

    def intenta():
        try:
            with fsutil.locked(dest, timeout=0.3):
                resultado.append("adquirido")
        except TimeoutError as exc:
            resultado.append(exc)

    with fsutil.locked(dest):
        t = threading.Thread(target=intenta)
        t.start()
        t.join()
    assert len(resultado) == 1 and isinstance(resultado[0], TimeoutError)


def test_locked_crea_dir_padre(tmp_path):
    dest = tmp_path / "sub" / "state.json"
    with fsutil.locked(dest):
        pass
    assert (tmp_path / "sub").is_dir()


def test_locked_libera_aunque_el_cuerpo_reviente(tmp_path):
    dest = tmp_path / "state.json"
    with pytest.raises(RuntimeError):
        with fsutil.locked(dest):
            raise RuntimeError("boom")
    with fsutil.locked(dest, timeout=0.5):  # no quedó tomado
        pass
