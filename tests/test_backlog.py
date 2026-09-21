"""Tests del scanner de deuda (backlog). Sin cuota."""

from agent.backlog import scan_repo


def test_detecta_deuda_basica(tmp_path):
    (tmp_path / "m.py").write_text("def f(a):\n    return a\n", encoding="utf-8")
    cands = scan_repo(tmp_path)
    assert cands and cands[0].path == "m.py"
    assert cands[0].score >= 2  # sin type hints + sin docstring


def test_no_excluye_por_skip_dir_ancestro(tmp_path):
    # el repo vive bajo un 'worktrees/' (como .claude/worktrees/): NO debe excluir todo el repo
    repo = tmp_path / "worktrees" / "proj"
    repo.mkdir(parents=True)
    (repo / "m.py").write_text("def f(a):\n    return a\n", encoding="utf-8")
    assert any(c.path == "m.py" for c in scan_repo(repo))


def test_excluye_skip_dirs_internos(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("def f(a):\n    return a\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "lib.py").write_text("def g(a):\n    return a\n", encoding="utf-8")
    paths = {c.path for c in scan_repo(tmp_path)}
    assert not any(".venv" in p for p in paths)  # el .venv interno sí se excluye


def test_ignora_tests(tmp_path):
    (tmp_path / "test_x.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    assert scan_repo(tmp_path) == []  # los tests no se auto-optimizan
