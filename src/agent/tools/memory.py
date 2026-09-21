"""Memoria de largo plazo: recall (lectura) y write (escritura) sobre el vault.

- ``recall_memory`` (Anillo 0, read-only): reusa ``~/.claude/tools/recall_memory.py``
  via subprocess. Una sola fuente de verdad, ya runtime-agnostico.
- ``write_memory`` (Anillo 1, con gate): crea una nota nueva con el patron
  una-idea-por-archivo + frontmatter, acotada al vault (nunca toca codigo).

La logica pura (``run_recall``, ``build_note``, ``resolve_note_path``, ...) es
testeable sin el SDK; los ``@tool`` son wrappers delgados.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from agent import config, fsutil

VALID_TYPES = {"user", "feedback", "project", "reference"}
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:[_-][a-z0-9]+)*$")


# --------------------------------------------------------------------------- #
# Logica pura (testeable sin SDK)
# --------------------------------------------------------------------------- #
def run_recall(
    query: str,
    exclude_project: str | None = None,
    *,
    script: Path | None = None,
    timeout: float = 30.0,
) -> str:
    """Ejecuta el motor de recall y devuelve su stdout (markdown)."""
    script = script or config.RECALL_SCRIPT
    if not Path(script).exists():
        return f"(motor de recall no encontrado: {script})"
    cmd = [sys.executable, str(script), query]
    if exclude_project:
        cmd += ["--exclude-project", exclude_project]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return "(recall excedio el timeout)"
    return (out.stdout or "").strip() or "(sin coincidencias)"


def validate_slug(name: str) -> str:
    """Valida un slug kebab/snake-case. Devuelve el slug o lanza ValueError."""
    if not _SLUG_RE.match(name or ""):
        raise ValueError(f"nombre invalido (usa kebab-case a-z0-9-): {name!r}")
    return name


def build_note(name: str, description: str, type_: str, body: str) -> str:
    """Construye el contenido de una nota (frontmatter + cuerpo)."""
    validate_slug(name)
    if type_ not in VALID_TYPES:
        raise ValueError(f"type invalido: {type_!r} (usa {sorted(VALID_TYPES)})")
    description = " ".join((description or "").split())
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "metadata:\n"
        f"  type: {type_}\n"
        "---\n\n"
        f"{(body or '').strip()}\n"
    )


def resolve_note_path(project: str, name: str, *, projects_dir: Path | None = None) -> Path:
    """Resuelve la ruta de una nota DENTRO del vault; rechaza traversal/proyecto ajeno."""
    base = (projects_dir or config.PROJECTS_DIR).resolve()
    proj_dir = (base / project).resolve()
    if proj_dir.parent != base:
        raise ValueError("proyecto invalido (fuera del vault)")
    mem = proj_dir / "memory"
    note = (mem / f"{validate_slug(name)}.md").resolve()
    if note.parent != mem.resolve():
        raise ValueError("ruta de nota invalida (traversal)")
    return note


_LINEA_NOTA_RE = re.compile(r"\]\(([^)]+?\.md)\)")
_CONTEO_RE = re.compile(r"(?:^|\s)(\d+)\s+notas\b")
_CONTEO_LINEAS_CABECERA = 10  # el conteo va en el preambulo, no mas abajo


def _sincroniza_conteo(lineas: list[str]) -> list[str]:
    """Reescribe el ``N notas`` de la cabecera con las notas que el indice enlaza.

    El validador del vault (``~/.claude/tools/validate_memory.py``) contrasta ese
    numero con los .md en disco: un indice que agrega una linea y deja el contador
    viejo lo rompe en la PRIMERA nota. El conteo es opcional en el contrato del
    indice, asi que si la cabecera no declara uno, esto no inventa ninguno.
    """
    enlazadas = {
        m.lower() for ln in lineas for m in _LINEA_NOTA_RE.findall(ln) if m.lower() != "memory.md"
    }
    for i, ln in enumerate(lineas[:_CONTEO_LINEAS_CABECERA]):
        m = _CONTEO_RE.search(ln)
        if m:
            lineas[i] = ln[: m.start(1)] + str(len(enlazadas)) + ln[m.end(1) :]
            break
    return lineas


def upsert_index(memory_dir: Path, title: str, name: str, hook: str) -> None:
    """Anade o ACTUALIZA la linea de la nota en el indice MEMORY.md (si el titulo/hook cambio)."""
    index = memory_dir / "MEMORY.md"
    line = f"- [{title}]({name}.md) — {' '.join((hook or '').split())}"
    lines = index.read_text(encoding="utf-8").splitlines() if index.exists() else []
    marker = f"]({name}.md)"
    for i, existing in enumerate(lines):
        if marker in existing:
            lines[i] = line  # actualiza en su sitio
            break
    else:
        lines.append(line)  # no estaba: la anade
    fsutil.write_text_atomic(index, "\n".join(_sincroniza_conteo(lines)) + "\n")


def remove_from_index(memory_dir: Path, name: str) -> None:
    """Quita la linea de la nota del indice MEMORY.md (para borrar o renombrar)."""
    index = memory_dir / "MEMORY.md"
    if not index.exists():
        return
    marker = f"]({name}.md)"
    kept = [ln for ln in index.read_text(encoding="utf-8").splitlines() if marker not in ln]
    kept = _sincroniza_conteo(kept)
    fsutil.write_text_atomic(index, ("\n".join(kept) + "\n") if kept else "")


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Extrae name/description/type del bloque frontmatter ``---`` de una nota."""
    m = re.match(r"^---\n(.*?)\n---", text or "", re.DOTALL)
    if not m:
        return {}
    block = m.group(1)
    fm: dict[str, str] = {}
    for key in ("name", "description"):
        km = re.search(rf"^{key}:\s*(.+)$", block, re.MULTILINE)
        if km:
            fm[key] = km.group(1).strip()
    # Anclado a inicio de linea: sin el ancla, "type:" engancha dentro de "node_type:"
    # (que las notas del vault traen ANTES) y el inventario reporta memory/note en vez
    # del type del contrato (user|feedback|project|reference).
    tm = re.search(r"^\s*type:\s*(\w+)", block, re.MULTILINE)  # anidado bajo metadata:
    if tm:
        fm["type"] = tm.group(1)
    return fm


def list_notes(
    project: str | None = None, *, projects_dir: Path | None = None
) -> list[dict[str, str]]:
    """Inventario del vault: (project, name, type, description) por cada nota (sin MEMORY.md)."""
    base = (projects_dir or config.PROJECTS_DIR).resolve()
    if not base.is_dir():
        return []
    proyectos = [base / project] if project else [p for p in sorted(base.iterdir()) if p.is_dir()]
    out: list[dict[str, str]] = []
    for proj in proyectos:
        mem = proj / "memory"
        if not mem.is_dir():
            continue
        for md in sorted(mem.glob("*.md")):
            if md.name == "MEMORY.md":
                continue
            fm = _parse_frontmatter(md.read_text(encoding="utf-8", errors="replace"))
            out.append(
                {
                    "project": proj.name,
                    "name": md.stem,
                    "type": fm.get("type", "?"),
                    "description": fm.get("description", ""),
                }
            )
    return out


def read_note(project: str, name: str, *, projects_dir: Path | None = None) -> str:
    """Contenido completo de una nota (para complementarla o revisarla antes de reescribir)."""
    note = resolve_note_path(project, name, projects_dir=projects_dir)
    if not note.exists():
        return f"(la nota no existe: {project}/{name})"
    return note.read_text(encoding="utf-8", errors="replace")


def delete_note(project: str, name: str, *, projects_dir: Path | None = None) -> Path:
    """Borra una nota del vault y la quita del indice. Lanza ValueError si no existe."""
    note = resolve_note_path(project, name, projects_dir=projects_dir)
    if not note.exists():
        raise ValueError(f"la nota no existe: {project}/{name}")
    note.unlink()
    remove_from_index(note.parent, name)
    return note


def archive_note(
    project: str, name: str, *, projects_dir: Path | None = None, suffix: str = "-archivo"
) -> Path:
    """Mueve una nota a un vault-historico (``<project><suffix>``): REVERSIBLE, no la pierde.

    Saca la nota del contexto principal (y de su indice) pero la conserva en un proyecto aparte,
    a diferencia de :func:`delete_note`. Lanza ValueError si la nota no existe.
    """
    src = resolve_note_path(project, name, projects_dir=projects_dir)
    if not src.exists():
        raise ValueError(f"la nota no existe: {project}/{name}")
    dst = resolve_note_path(project + suffix, name, projects_dir=projects_dir)
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.replace(dst)  # mover (atomico; conserva el contenido en el archivo)
    remove_from_index(src.parent, name)
    return dst


# --------------------------------------------------------------------------- #
# Tools del SDK (wrappers delgados)
# --------------------------------------------------------------------------- #
@tool(
    "recall_memory",
    "Busca en la memoria markdown de TODOS los proyectos del usuario (decisiones, "
    "flujos, gotchas). Read-only. Usa exclude_project para omitir un proyecto.",
    {"query": str, "exclude_project": str},
)
async def recall_memory(args: dict[str, Any]) -> dict[str, Any]:
    text = run_recall(args.get("query", ""), (args.get("exclude_project") or "").strip() or None)
    return {"content": [{"type": "text", "text": text}]}


@tool(
    "write_memory",
    "Crea una nota NUEVA en el vault (patron una-idea-por-archivo). Acotada al vault, "
    "nunca toca codigo. type: user|feedback|project|reference.",
    {
        "project": str,
        "name": str,
        "title": str,
        "description": str,
        "type": str,
        "body": str,
    },
)
async def write_memory(args: dict[str, Any]) -> dict[str, Any]:
    try:
        note = resolve_note_path(args["project"], args["name"])
        content = build_note(args["name"], args["description"], args["type"], args["body"])
        fsutil.write_text_atomic(note, content)
        upsert_index(
            note.parent, args.get("title") or args["name"], args["name"], args["description"]
        )
        return {"content": [{"type": "text", "text": f"Nota escrita: {note}"}]}
    except Exception as exc:  # noqa: BLE001 - devolver el error como dato, no matar el loop
        return {"content": [{"type": "text", "text": f"Error: {exc}"}], "is_error": True}


@tool(
    "list_memory",
    "Inventario de la memoria: lista las notas (nombre, tipo, descripcion) de un proyecto o de "
    "todos si no se indica. Read-only. Usalo ANTES de compilar/organizar para saber que ya existe.",
    {"project": str},
)
async def list_memory(args: dict[str, Any]) -> dict[str, Any]:
    notas = list_notes((args.get("project") or "").strip() or None)
    if not notas:
        return {"content": [{"type": "text", "text": "(sin notas)"}]}
    por_proj: dict[str, list[dict[str, str]]] = {}
    for n in notas:
        por_proj.setdefault(n["project"], []).append(n)
    bloques = [
        f"## {proj} ({len(items)})\n"
        + "\n".join(f"  - {i['name']} [{i['type']}] — {i['description']}" for i in items)
        for proj, items in por_proj.items()
    ]
    return {"content": [{"type": "text", "text": "\n\n".join(bloques)}]}


@tool(
    "read_memory",
    "Lee el contenido completo de una nota por proyecto y nombre (slug). Read-only. Usalo para "
    "complementar una nota sin perder lo que ya tenia.",
    {"project": str, "name": str},
)
async def read_memory(args: dict[str, Any]) -> dict[str, Any]:
    text = read_note(args.get("project", ""), args.get("name", ""))
    return {"content": [{"type": "text", "text": text}]}


@tool(
    "delete_memory",
    "Borra una nota de la memoria y la quita del indice. Accion con efectos (para organizar/"
    "depurar notas obsoletas o duplicadas).",
    {"project": str, "name": str},
)
async def delete_memory(args: dict[str, Any]) -> dict[str, Any]:
    try:
        note = delete_note(args["project"], args["name"])
        return {"content": [{"type": "text", "text": f"Nota borrada: {note}"}]}
    except Exception as exc:  # noqa: BLE001 - devolver el error como dato, no matar el loop
        return {"content": [{"type": "text", "text": f"Error: {exc}"}], "is_error": True}


@tool(
    "list_repos",
    "Lista los repositorios que gestiona el usuario (nombre y ruta), para tareas cross-repo "
    "(compilar memoria, auditar). Read-only.",
    {},
)
async def list_repos(args: dict[str, Any]) -> dict[str, Any]:
    lineas = "\n".join(f"- {n}: {p}" for n, p in config.REPOS.items())
    return {"content": [{"type": "text", "text": lineas or "(sin repos configurados)"}]}


memory_server = create_sdk_mcp_server(
    "memory",
    version="0.1.0",
    tools=[recall_memory, write_memory, list_memory, read_memory, delete_memory, list_repos],
)
