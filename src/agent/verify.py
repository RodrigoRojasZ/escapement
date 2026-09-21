"""Verificación de verdad (P0-1): baseline + diff de API pública + veredicto.

El problema que resuelve: ``pytest -q`` a secas es ciego. Sin tests devuelve "no ran"
(no verifica nada); sin baseline no distingue un fallo introducido por el refactor de
uno pre-existente. Aquí el baseline se corre ANTES del refactor y se compara; cuando no
hay tests fiables, el diff de firmas públicas (AST) actúa como red de seguridad.
"""

from __future__ import annotations

import ast
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


def run_pytest(
    repo: Path | str,
    python_exe: str = "python",
    timeout: float = 300,
    test_path: str | None = None,
) -> tuple[bool | None, str]:
    """Corre pytest. Devuelve (veredicto, salida): True=pasan, False=fallan, None=no hay tests.

    Args:
        test_path: si se indica, acota pytest a esos tests en vez de toda la suite — clave en
            repos grandes cuya suite completa requiere Chrome/DB/proxies. None = toda la suite.
    """
    cmd = [python_exe, "-m", "pytest", "-q"]
    if test_path:
        cmd.append(test_path)
    r = subprocess.run(
        cmd,
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode == 5:  # pytest: "no tests collected"
        return None, out
    return r.returncode == 0, out


def public_api(path: Path | str) -> dict[str, str]:
    """Firmas de funciones/clases públicas top-level de un .py: ``{nombre: firma}``."""
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, OSError):
        return {}
    api: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and not node.name.startswith(
            "_"
        ):
            params = [
                a.arg for a in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            ]
            api[node.name] = "(" + ", ".join(params) + ")"
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            methods = sorted(
                n.name
                for n in node.body
                if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
                and not n.name.startswith("_")
            )
            api[node.name] = "class{" + ",".join(methods) + "}"
    return api


@dataclass
class Verification:
    tests_verdict: str
    tests_ok: bool | None
    api_preserved: bool
    api_changes: list[str] = field(default_factory=list)
    ok: bool = False


def verify(
    repo: Path | str,
    target: str,
    baseline_tests: bool | None,
    baseline_api: dict[str, str],
    python_exe: str = "python",
    test_path: str | None = None,
) -> Verification:
    """Compara el estado post-refactor contra el baseline y decide si es seguro para PR."""
    after_tests, _ = run_pytest(repo, python_exe, test_path=test_path)
    tgt = Path(repo) / target
    after_api = public_api(tgt) if tgt.is_file() else {}

    if baseline_tests is None and after_tests is None:
        verdict, tests_ok = "sin tests (decide la red estática de API)", None
    elif baseline_tests is False:
        verdict, tests_ok = "los tests ya fallaban antes (no atribuible al refactor)", None
    elif after_tests is True:
        verdict, tests_ok = "preservado: los tests pasan", True
    else:
        verdict, tests_ok = "ROTO: tests que pasaban ahora fallan", False

    changed = {k for k in baseline_api if baseline_api.get(k) != after_api.get(k)}
    api_changes = [
        f"{k}: {baseline_api[k]} -> {after_api.get(k, 'ELIMINADO')}" for k in sorted(changed)
    ]
    api_preserved = not api_changes

    if tests_ok is True:
        ok = True  # los tests son el oráculo: si pasan, es seguro
    elif tests_ok is False:
        ok = False  # el refactor rompió tests que pasaban
    else:
        ok = api_preserved  # sin oráculo de tests, la API pública es la red

    return Verification(verdict, tests_ok, api_preserved, api_changes, ok)
