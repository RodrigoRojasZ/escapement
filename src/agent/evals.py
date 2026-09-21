"""Evals: mide la CALIDAD/mejora de un refactor (la pata de 'medir'), no solo que no rompió.

Métricas objetivas y baratas (sin LLM/cuota), antes vs después: cobertura de type hints y
docstrings (AST), issues de ruff (si está), y líneas. El veredicto de mejora se registra en
el ledger -> Escapement acumula evidencia de qué tareas rinden (base para aprender/priorizar).
"""

from __future__ import annotations

import ast
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_RUFF = shutil.which("ruff")


def metrics(path: Path | str) -> dict[str, float]:
    """Métricas objetivas de un .py: líneas, cobertura de type hints y docstrings."""
    src = Path(path).read_text(encoding="utf-8", errors="replace")
    lines = float(src.count("\n") + 1)
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {"lines": lines, "hint_cov": 0.0, "doc_cov": 0.0, "funcs": 0.0}
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
    if not funcs:
        return {"lines": lines, "hint_cov": 1.0, "doc_cov": 1.0, "funcs": 0.0}
    hinted = sum(1 for f in funcs if f.returns or any(a.annotation for a in f.args.args))
    documented = sum(1 for f in funcs if ast.get_docstring(f))
    return {
        "lines": lines,
        "hint_cov": hinted / len(funcs),
        "doc_cov": documented / len(funcs),
        "funcs": float(len(funcs)),
    }


def ruff_issues(repo: Path | str, target: str) -> int:
    """Número aproximado de issues de ruff en el target (-1 si ruff no está disponible)."""
    if not _RUFF:
        return -1
    try:
        r = subprocess.run(
            [_RUFF, "check", "--output-format=concise", target],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=60,
        )
        return sum(1 for line in r.stdout.splitlines() if line.count(":") >= 2)
    except Exception:
        return -1


@dataclass
class Eval:
    summary: str
    improved: bool


def evaluate(before: dict, after: dict, ruff_before: int = -1, ruff_after: int = -1) -> Eval:
    """Compara métricas antes/después y decide si hubo mejora objetiva (sin regresión)."""
    hint_delta = after.get("hint_cov", 0.0) - before.get("hint_cov", 0.0)
    doc_delta = after.get("doc_cov", 0.0) - before.get("doc_cov", 0.0)
    ruff_ok = ruff_before < 0 or ruff_after < 0 or ruff_after <= ruff_before
    improved = (
        ruff_ok
        and hint_delta >= 0
        and doc_delta >= 0
        and (hint_delta > 0 or doc_delta > 0 or (ruff_before > ruff_after >= 0))
    )
    parts = [
        f"type hints {before.get('hint_cov', 0):.0%}->{after.get('hint_cov', 0):.0%}",
        f"docstrings {before.get('doc_cov', 0):.0%}->{after.get('doc_cov', 0):.0%}",
    ]
    if ruff_before >= 0 and ruff_after >= 0:
        parts.append(f"ruff {ruff_before}->{ruff_after}")
    return Eval("; ".join(parts), improved)
