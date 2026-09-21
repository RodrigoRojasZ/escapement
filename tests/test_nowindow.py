"""Test del patch anti-consola (best-effort; el efecto real solo aplica en Windows)."""

from agent.voice import _nowindow


def test_suppress_child_consoles_noop_fuera_de_win32(monkeypatch):
    monkeypatch.setattr(_nowindow, "_patched", False)
    monkeypatch.setattr(_nowindow.sys, "platform", "linux")
    _nowindow.suppress_child_consoles()
    assert _nowindow._patched is False  # no parchea subprocess fuera de Windows
