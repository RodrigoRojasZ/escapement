"""Tests de integración del pipeline del orquestador. Sin cuota ni red.

Ejercen el flujo REAL de ``optimize`` (worktree efímero + git + verify + eval + ledger) y de
``run_queue`` (cola + throttle) sobre un repo git de fixture. Sólo se mockean los dos puntos
que gastan cuota/red: el ``dispatch`` a Claude (se simula el refactor editando el archivo) y el
``judge_diff`` adversarial. El resto corre de verdad.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from agent import config, judge, ledger, orchestrator, state


def _git_repo(path: Path, files: dict[str, str]) -> Path:
    """Crea un repo git real con ``files`` y un commit inicial."""
    path.mkdir(parents=True, exist_ok=True)
    for cmd in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *cmd], cwd=path, check=True, capture_output=True)
    for name, content in files.items():
        (path / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True, capture_output=True)
    return path


@pytest.fixture
def aislado(tmp_path, monkeypatch):
    """Aísla ledger y state en archivos temporales (no toca los reales)."""
    monkeypatch.setattr(config, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(state, "STATE", tmp_path / "state.json")


def test_optimize_pipeline_veredicto_seguro(tmp_path, monkeypatch, aislado):
    repo = _git_repo(tmp_path / "proj", {"mod.py": "def f(a):\n    return a * 2\n"})

    def fake_dispatch(work, prompt, timeout=900, allow_secrets=False):
        # simula el refactor de Claude: añade type hints y docstring, preserva la API pública.
        (Path(work) / "mod.py").write_text(
            'def f(a: int) -> int:\n    """Duplica el valor."""\n    return a * 2\n',
            encoding="utf-8",
        )
        return True, "refactor simulado"

    monkeypatch.setattr(orchestrator, "dispatch", fake_dispatch)
    monkeypatch.setattr(judge, "judge_diff", lambda diff, directiva, **k: judge.Judgment("SEGURO"))

    res = orchestrator.optimize(
        repo, "añade type hints y docstring", "mod.py", python_exe=sys.executable, make_pr=False
    )

    assert res.dispatched is True
    assert res.verified is True  # sin tests + API preservada + juez SEGURO
    assert res.branch.startswith("escapement/optimiza-mod-")
    assert "mod.py" in res.diff_stat
    rows = ledger.read()
    assert rows and rows[-1]["action"] == "optimize" and rows[-1]["verified"] is True
    # el worktree efímero se removió: sólo queda el principal
    wt = subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True)
    assert len(wt.stdout.strip().splitlines()) == 1


def test_optimize_rechaza_ruptura_de_api(tmp_path, monkeypatch, aislado):
    repo = _git_repo(tmp_path / "proj", {"mod.py": "def f(a):\n    return a\n"})

    def fake_break(work, prompt, timeout=900, allow_secrets=False):
        # renombra la función pública f -> g: rompe la API observable.
        (Path(work) / "mod.py").write_text("def g(a):\n    return a\n", encoding="utf-8")
        return True, "cambió la API"

    monkeypatch.setattr(orchestrator, "dispatch", fake_break)
    monkeypatch.setattr(judge, "judge_diff", lambda *a, **k: judge.Judgment("SEGURO"))

    res = orchestrator.optimize(
        repo, "refactor", "mod.py", python_exe=sys.executable, make_pr=False
    )

    assert res.verified is False  # la API pública cambió (f -> g)
    assert "API cambió" in res.verdict


def test_run_queue_procesa_toda_la_cola(monkeypatch, aislado):
    state.enqueue(
        [
            {"repo": "R", "target": "a.py", "directiva": "x", "base": "main"},
            {"repo": "R", "target": "b.py", "directiva": "y", "base": "main"},
        ]
    )

    def fake_opt(repo, directiva, target, **k):
        return orchestrator.Result(
            branch="b",
            dispatched=True,
            tests_ok=True,
            verified=True,
            verdict="ok",
            diff_stat="",
            summary="ok",
            pr_url="http://pr/1",
        )

    monkeypatch.setattr(orchestrator, "optimize", fake_opt)

    r = orchestrator.run_queue(limit=0, python_exe=sys.executable)

    assert r["procesadas"] == 2 and r["pendientes"] == 0 and r["throttled"] is False
    assert all(t["status"] == "seguro" for t in state._load()["queue"])


def test_run_queue_pausa_en_rate_limit(monkeypatch, aislado):
    state.enqueue(
        [
            {"repo": "R", "target": "a.py", "directiva": "x", "base": "main"},
            {"repo": "R", "target": "b.py", "directiva": "y", "base": "main"},
        ]
    )

    def fake_ratelimited(repo, directiva, target, **k):
        return orchestrator.Result(
            branch="b",
            dispatched=False,
            tests_ok=None,
            verified=False,
            verdict="",
            diff_stat="",
            summary="usage limit reached, try again later",
            pr_url=None,
        )

    monkeypatch.setattr(orchestrator, "optimize", fake_ratelimited)

    r = orchestrator.run_queue(limit=0, python_exe=sys.executable)

    assert r["throttled"] is True
    assert r["procesadas"] == 0
    assert len(state.pending()) == 2  # ninguna consumida: quedan para reanudar


def test_optimize_bloquea_target_con_secretos(tmp_path, monkeypatch, aislado):
    # un archivo con un secreto hardcodeado NO debe despacharse a la API del executor (P1)
    repo = _git_repo(
        tmp_path / "proj", {"cfg.py": 'DB_URL = "mysql://root:realPass123@h:3306/db"\n'}
    )
    llamado = []
    monkeypatch.setattr(orchestrator, "dispatch", lambda *a, **k: llamado.append(1) or (True, ""))

    res = orchestrator.optimize(repo, "opt", "cfg.py", python_exe=sys.executable, make_pr=False)

    assert res.verified is False and "BLOQUEADO" in res.verdict
    assert not res.dispatched and not llamado  # no se despachó nada


def test_run_queue_un_fallo_no_tumba_la_cola(monkeypatch, aislado):
    # Fase 0c: si optimize revienta en una tarea, la cola sigue con las demás (no crashea).
    state.enqueue(
        [
            {"repo": "R", "target": "boom.py", "directiva": "x", "base": "main"},
            {"repo": "R", "target": "ok.py", "directiva": "y", "base": "main"},
        ]
    )

    def opt(repo, directiva, target, **k):
        if target == "boom.py":
            raise RuntimeError("git reventó")
        return orchestrator.Result(
            branch="b",
            dispatched=True,
            tests_ok=True,
            verified=True,
            verdict="ok",
            diff_stat="",
            summary="ok",
            pr_url="http://pr/1",
        )

    monkeypatch.setattr(orchestrator, "optimize", opt)

    r = orchestrator.run_queue(limit=0, python_exe=sys.executable)

    assert r["procesadas"] == 1  # ok.py salió pese al fallo de boom.py
    assert r["fallidas"] == 0  # boom.py falló 1 vez (< MAX): vuelve a pending, aún no terminal
    q = {t["target"]: t for t in state._load()["queue"]}
    assert q["ok.py"]["status"] == "seguro"
    assert q["boom.py"]["status"] == "pending" and q["boom.py"]["attempts"] == 1
    errores = [row for row in ledger.read() if row.get("error")]
    assert errores and "RuntimeError" in errores[-1]["error"]


def test_run_queue_marca_fallido_tras_agotar_intentos(monkeypatch, aislado):
    # Poison-pill: una tarea que siempre falla no se re-despacha para siempre -> "fallido" terminal.
    state.enqueue([{"repo": "R", "target": "boom.py", "directiva": "x", "base": "main"}])
    tid = state.pending()[0]["id"]
    state.mark(tid, "pending", attempts=1)  # simula que ya falló una vez antes

    monkeypatch.setattr(
        orchestrator, "optimize", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("otra vez"))
    )

    r = orchestrator.run_queue(limit=0, python_exe=sys.executable)

    assert r["fallidas"] == 1 and r["pendientes"] == 0
    t = state._load()["queue"][0]
    assert t["status"] == "fallido" and t["attempts"] == 2


def test_resolve_repo_nombre_ruta_y_slug(tmp_path, monkeypatch):
    # Fuente única de resolución para el CLI y las tools de chat/voz: nombre, ruta o slug.
    repo = _git_repo(tmp_path / "proj", {"mod.py": "x = 1\n"})
    monkeypatch.setattr(config, "REPOS", {"proj": str(repo)})
    assert orchestrator.resolve_repo("proj") == str(repo)  # nombre registrado: valor tal cual
    # rutas: se sube a la raíz git (git responde con '/', por eso se compara como Path)
    assert Path(orchestrator.resolve_repo(str(repo / "mod.py"))).resolve() == repo.resolve()
    assert Path(orchestrator.resolve_repo(str(repo))).resolve() == repo.resolve()
    assert orchestrator.resolve_repo("plan-de-otro-lado") == "plan-de-otro-lado"  # slug: literal


def test_cli_resolve_repo_delega_en_el_core(tmp_path, monkeypatch):
    from agent import cli

    repo = _git_repo(tmp_path / "proj2", {"mod.py": "x = 1\n"})
    monkeypatch.setattr(config, "REPOS", {"p2": str(repo)})
    for arg in ("p2", str(repo / "mod.py"), "slug-suelto"):
        assert cli._resolve_repo(arg) == orchestrator.resolve_repo(arg)
