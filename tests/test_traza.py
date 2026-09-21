"""Tests de trazabilidad del plan (traza en árbol) y rescate a una rama de entrega.

- ``arbol_archivos`` es una función PURA: se testea sin git.
- ``archivos_del_plan``/``default_branch``/``traspasar_a_rama`` operan sobre git: se testean contra
  un repo git REAL efímero (``tmp_path``), sin red/Chrome/DB. Se saltan si git no está disponible.
"""

import shutil
import subprocess

import pytest

from agent import runner
from agent.planner import Plan

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git no disponible")


# --- arbol_archivos (pura, sin git) --------------------------------------------


def test_arbol_archivos_vacio():
    assert runner.arbol_archivos([]) == ""


def test_arbol_archivos_una_hoja():
    assert runner.arbol_archivos(["a.py"]) == "└─ a.py"


def test_arbol_archivos_agrupa_y_ordena_carpetas_antes_de_archivos():
    rutas = ["storage/s3.py", "storage/azure_blob.py", "raiz.py", "tests/test_s3.py"]
    esperado = "\n".join(
        [
            "├─ storage/",
            "│  ├─ azure_blob.py",
            "│  └─ s3.py",
            "├─ tests/",
            "│  └─ test_s3.py",
            "└─ raiz.py",
        ]
    )
    assert runner.arbol_archivos(rutas) == esperado


def test_arbol_archivos_normaliza_backslash_y_dedup():
    # separador Windows y duplicados: se normaliza a '/' y se colapsa
    assert runner.arbol_archivos(["a\\b.py", "a/b.py"]) == "└─ a/\n   └─ b.py"


# --- helpers de git para los tests de integración ------------------------------


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _repo(tmp_path):
    """Repo git efímero con un commit base en la rama 'main'."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t.co")
    _git(repo, "config", "user.name", "tester")
    (repo / "readme.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    return repo


def _ls_tree(repo, ref):
    out = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", ref],
        capture_output=True,
        text=True,
    )
    return out.stdout.split()


# --- default_branch ------------------------------------------------------------


def test_default_branch_detecta_main(tmp_path):
    repo = _repo(tmp_path)
    assert runner.default_branch(str(repo)) == "main"


def test_default_branch_repo_no_git_devuelve_vacio(tmp_path):
    d = tmp_path / "nogit"
    d.mkdir()
    assert runner.default_branch(str(d)) == ""


# --- archivos_del_plan ---------------------------------------------------------


def test_archivos_del_plan_sin_worktree_es_vacio():
    plan = Plan("obj", "crit")  # workdir vacío
    assert runner.archivos_del_plan(plan) == []


def test_archivos_del_plan_incluye_untracked_expandiendo_carpetas(tmp_path):
    import pathlib

    repo = _repo(tmp_path)
    plan = Plan("obj", "crit", repo=str(repo))
    wt = runner._worktree_plan(plan, str(repo))
    assert wt
    (pathlib.Path(wt) / "nuevo.py").write_text("x = 1\n", encoding="utf-8")
    sub = pathlib.Path(wt) / "sub"
    sub.mkdir()
    (sub / "otro.py").write_text("y = 2\n", encoding="utf-8")
    rutas = runner.archivos_del_plan(plan)
    assert "nuevo.py" in rutas
    assert "sub/otro.py" in rutas  # carpeta untracked EXPANDIDA (no 'sub/')


def test_archivos_del_plan_incluye_lo_commiteado_en_la_rama(tmp_path):
    import pathlib

    repo = _repo(tmp_path)
    plan = Plan("obj", "crit", repo=str(repo))
    wt = runner._worktree_plan(plan, str(repo))
    (pathlib.Path(wt) / "commiteado.py").write_text("z = 3\n", encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "paso 1")
    assert "commiteado.py" in runner.archivos_del_plan(plan)


# --- traspasar_a_rama ----------------------------------------------------------


def test_traspasar_sin_worktree_falla_limpio():
    res = runner.traspasar_a_rama(Plan("obj", "crit"), "/no/existe")
    assert res.ok is False and "worktree" in res.motivo


def test_traspasar_crea_rama_sobre_default_sin_tocar_base(tmp_path):
    import pathlib

    repo = _repo(tmp_path)
    plan = Plan("integrar S3 de prueba", "crit", repo=str(repo))
    wt = runner._worktree_plan(plan, str(repo))
    (pathlib.Path(wt) / "feature.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    res = runner.traspasar_a_rama(plan, str(repo))

    assert res.ok, res.motivo
    assert res.base == "main"
    assert res.rama.startswith("escapement/entrega-")
    assert "feature.py" in res.archivos
    # la rama de entrega = base + delta del plan
    entrega = _ls_tree(repo, res.rama)
    assert "feature.py" in entrega
    assert "readme.md" in entrega  # heredó la base
    # main quedó intacta (sin el archivo del plan)
    assert "feature.py" not in _ls_tree(repo, "main")
    # el worktree efímero se desmontó (solo queda el del plan)
    wts = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True
    ).stdout
    assert "escapement-entrega-" not in wts


def test_traspasar_mezcla_commiteado_y_sin_commitear(tmp_path):
    import pathlib

    repo = _repo(tmp_path)
    plan = Plan("mezcla wip y commit", "crit", repo=str(repo))
    wt = runner._worktree_plan(plan, str(repo))
    # una parte commiteada en la rama del plan...
    (pathlib.Path(wt) / "commiteado.py").write_text("a = 1\n", encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "paso 1")
    # ...y otra aún sin commitear
    (pathlib.Path(wt) / "wip.py").write_text("b = 2\n", encoding="utf-8")

    res = runner.traspasar_a_rama(plan, str(repo))
    assert res.ok, res.motivo
    entrega = _ls_tree(repo, res.rama)
    assert "commiteado.py" in entrega  # lo ya commiteado
    assert "wip.py" in entrega  # lo pendiente, commiteado por el rescate (nada se pierde)


def test_traspasar_delta_vacio_no_crea_rama(tmp_path):
    repo = _repo(tmp_path)
    plan = Plan("sin cambios", "crit", repo=str(repo))
    runner._worktree_plan(plan, str(repo))  # worktree sin cambios
    res = runner.traspasar_a_rama(plan, str(repo))
    assert res.ok is False and "delta vacío" in res.motivo
