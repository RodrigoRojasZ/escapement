"""Gate de confirmacion para acciones con efectos (Anillos 1-3), por consola.

Las tools del Anillo 0 van en ``allowed_tools`` y se auto-aprueban (nunca llegan aca). Todo lo
demas —escribir memoria, operar el orquestador, shell, control del sistema— cae a este gate, que
pide confirmacion explicita y devuelve ``Allow``/``Deny``. Fail-safe: si no hay TTY, deniega.

La pregunta es LEGIBLE: :func:`_describe` resume la accion en pocas palabras
(``optimizar worker/x.py``, ``ejecutar: git status``) en vez de volcar el JSON de argumentos.
:class:`TextGate` recuerda las categorias ya aprobadas (aprobar-una-vez): la 1a accion de cada
tipo se confirma; al aceptar, las siguientes de esa categoria se auto-aprueban en la sesion.
El :func:`~agent.voice.interaction.VoiceGate` es su gemelo hablado y comparte :func:`_category`.
El guard hook (SQL/ramas/.env) bloquea aparte, sin importar el gate.
"""

from __future__ import annotations

import asyncio
from typing import Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

from agent.config import AGENT_NAME

_YES = {
    "s",
    "si",
    "sí",
    "y",
    "yes",
    "ok",
    "dale",
    "bien",
    "correcto",
    "confirmo",
    "autorizo",
    "permito",
    "bueno",
    "adelante",
    "prosiga",
    "continua",
    "continúe",
    "sigue",
    "siga",
}


def _category(tool_name: str) -> str:
    """Categoria de permiso de una tool (agrupa las afines para el aprobar-una-vez).

    Compartida por el gate de texto y el de voz para que una sola aprobacion cubra el tipo.
    """
    if tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return "edit"
    if tool_name in ("Bash", "PowerShell"):
        return "shell"
    if tool_name.startswith("mcp__memory__write"):
        return "memory"
    if tool_name.startswith("mcp__orq__"):
        return "orq"
    return tool_name  # sin agrupar: se confirma por su propio nombre


# Nombre corto y legible de cada categoria (para el "un sí cubre …").
_CATEGORY_SHORT: dict[str, str] = {
    "edit": "edición de archivos",
    "shell": "terminal",
    "memory": "memoria",
    "orq": "orquestador",
}


def _describe(tool_name: str, input_data: dict[str, Any]) -> str:
    """Resumen corto y legible (sin JSON) de la accion a confirmar."""
    cat = _category(tool_name)
    if cat == "shell":
        cmd = " ".join(str(input_data.get("command", "")).split())
        return f"ejecutar: {cmd[:60]}" if cmd else "ejecutar un comando en la terminal"
    if cat == "edit":
        return f"editar {input_data.get('file_path', '(archivo)')}"
    if cat == "orq":
        accion = tool_name.split("__")[-1].replace("_", " ")  # optimizar / revisar prs / ...
        objetivo = input_data.get("target") or input_data.get("repo") or ""
        return f"{accion} {objetivo}".strip()
    if cat == "memory":
        return "guardar una nota en tu memoria"
    return f"usar {tool_name}"


class TextGate:
    """Gate por consola con memoria por categoria (aprobar una vez, extender a la sesion)."""

    def __init__(self) -> None:
        self.approved: set[str] = set()

    async def __call__(
        self,
        tool_name: str,
        input_data: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        cat = _category(tool_name)
        if cat in self.approved:
            return PermissionResultAllow()  # ya autorizado en esta sesion: no re-preguntar
        etiqueta = _CATEGORY_SHORT.get(cat, cat)
        prompt = (
            f"\n[{AGENT_NAME}] ¿Autorizo {_describe(tool_name, input_data)}?\n"
            f"  (un sí cubre las acciones de «{etiqueta}» en esta sesión)  [s/N] "
        )
        try:
            loop = asyncio.get_running_loop()
            answer = await loop.run_in_executor(None, lambda: input(prompt))
        except (EOFError, RuntimeError, OSError):
            return PermissionResultDeny(message="Sin TTY para confirmar; denegado por seguridad.")
        if answer.strip().lower() in _YES:
            self.approved.add(cat)  # extiende el permiso a las próximas de la misma categoría
            return PermissionResultAllow()
        return PermissionResultDeny(message="Rechazado por el usuario.")


async def confirm_action(
    tool_name: str,
    input_data: dict[str, Any],
    context: ToolPermissionContext,
) -> PermissionResultAllow | PermissionResultDeny:
    """Confirmacion por consola de un solo uso (sin memoria de categoria). Para llamadas sueltas."""
    return await TextGate()(tool_name, input_data, context)
