"""Backlog-generator (la auto-evolución): escanea repos y se auto-asigna mejoras.

Heurística barata (AST + regex, sin LLM ni cuota): detecta deuda técnica —funciones
sin type hints ni docstrings, TODOs/FIXMEs, archivos largos— y produce una cola de
candidatos priorizados que el orquestador (:func:`escapement.orchestrator.optimize`)
procesa en cadena, cada uno en su propio PR.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

_SKIP_DIRS = {
    ".venv",
    "venv",
    "__pycache__",
    ".git",
    "node_modules",
    "build",
    "dist",
    ".ruff_cache",
    ".pytest_cache",
    "libs",
    "models",
    "worktrees",
}
# Solo en comentarios, para no contar las keywords cuando aparecen en strings/código.
_TODO = re.compile(r"#.*\b(TODO|FIXME|XXX|HACK)\b")


@dataclass
class Candidate:
    path: str  # relativa al repo
    score: int
    reasons: list[str]


def _analyze(src: str) -> tuple[int, list[str]]:
    """Puntúa la deuda técnica de un módulo Python. Devuelve (score, razones)."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return 0, []
    funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    no_hints = [
        f
        for f in funcs
        if f.returns is None and not any(a.annotation for a in f.args.args) and f.name != "__init__"
    ]
    no_docs = [f for f in funcs if ast.get_docstring(f) is None and not f.name.startswith("_")]
    todos = len(_TODO.findall(src))
    lines = src.count("\n") + 1

    score, reasons = 0, []
    if no_hints:
        score += len(no_hints)
        reasons.append(f"{len(no_hints)} func sin type hints")
    if no_docs:
        score += len(no_docs)
        reasons.append(f"{len(no_docs)} func sin docstring")
    if todos:
        score += todos
        reasons.append(f"{todos} TODO/FIXME")
    if lines > 400:
        score += 2
        reasons.append(f"{lines} líneas (archivo grande)")
    return score, reasons


def scan_repo(repo: Path | str, limit: int = 20) -> list[Candidate]:
    """Escanea los .py del repo y devuelve candidatos de mejora (más deuda primero)."""
    repo = Path(repo)
    candidates: list[Candidate] = []
    for py in repo.rglob("*.py"):
        rel = py.relative_to(repo)
        # Excluir _SKIP_DIRS DENTRO del repo (partes de `rel`), NO del path absoluto que lleva
        # a él: si el repo vive bajo p.ej. `.claude/worktrees/`, esas partes no deben excluir todo.
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        if py.name.startswith("test_") or py.name.endswith("_test.py") or py.name == "conftest.py":
            continue  # los tests son el oráculo de comportamiento: no se auto-optimizan
        try:
            src = py.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        score, reasons = _analyze(src)
        if score > 0:
            candidates.append(Candidate(str(rel), score, reasons))
    return sorted(candidates, key=lambda c: -c.score)[:limit]
