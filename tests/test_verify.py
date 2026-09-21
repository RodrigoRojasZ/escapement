"""Tests de la verificación de verdad (baseline + API-diff). Sin cuota, sin red."""

import sys

from agent.verify import public_api, run_pytest, verify

PY = sys.executable


def test_public_api_excluye_privados(tmp_path):
    (tmp_path / "m.py").write_text(
        "def f(a, b):\n    return a\n\n\ndef _p():\n    pass\n\n\n"
        "class C:\n    def m(self):\n        pass\n\n    def _h(self):\n        pass\n",
        encoding="utf-8",
    )
    assert public_api(tmp_path / "m.py") == {"f": "(a, b)", "C": "class{m}"}


def test_run_pytest_sin_tests(tmp_path):
    (tmp_path / "x.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    ok, _ = run_pytest(tmp_path, python_exe=PY)
    assert ok is None  # "no tests collected" != fallo


def test_run_pytest_pasan(tmp_path):
    (tmp_path / "test_x.py").write_text("def test_ok():\n    assert 1 == 1\n", encoding="utf-8")
    ok, _ = run_pytest(tmp_path, python_exe=PY)
    assert ok is True


def test_run_pytest_fallan(tmp_path):
    (tmp_path / "test_x.py").write_text("def test_bad():\n    assert 1 == 2\n", encoding="utf-8")
    ok, _ = run_pytest(tmp_path, python_exe=PY)
    assert ok is False


def test_verify_rechaza_api_break_sin_tests(tmp_path):
    # Sin tests, la red es el diff de API: si la firma pública cambió -> RECHAZADO.
    (tmp_path / "m.py").write_text("def f(a):\n    return a\n", encoding="utf-8")
    v = verify(tmp_path, "m.py", baseline_tests=None, baseline_api={"f": "(a, b)"}, python_exe=PY)
    assert v.tests_ok is None and not v.api_preserved and v.ok is False


def test_verify_ok_sin_tests_api_preservada(tmp_path):
    (tmp_path / "m.py").write_text("def f(a, b):\n    return a + b\n", encoding="utf-8")
    v = verify(tmp_path, "m.py", baseline_tests=None, baseline_api={"f": "(a, b)"}, python_exe=PY)
    assert v.api_preserved and v.ok is True
