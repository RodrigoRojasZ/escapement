"""Interacción verbal para el modo voz: confirmación hablada de acciones con efectos.

Reemplaza el ``input()`` por consola de :func:`agent.security.permissions.confirm_action`.
Dos mejoras clave sobre el modelo naïf de "preguntar por cada tool":

- **Lenguaje natural (no símbolos):** la pregunta hablada describe la acción en palabras
  ("modificar archivos", "ejecutar en la terminal git") en vez de leer rutas o comandos crudos
  con todos sus símbolos (``&``, ``$``, ``/``…). El detalle técnico va al ``print``, no al TTS.
- **Aprobar una vez por categoría (:class:`VoiceGate`):** la primera acción de cada categoría
  (editar archivos / terminal / memoria / orquestador) se confirma hablando; al aceptar, las
  siguientes de esa misma categoría se auto-aprueban en la sesión. Evita pedir permiso 12 veces
  para el mismo tipo de acción. ``reset()`` al cambiar de tema.

Fail-safe: si no entiende, no escucha, o expira el tiempo de espera, deniega.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

from agent import config
from agent.security.permissions import _category
from agent.voice import ears, tts

# Instrucción que se añade al system prompt en modo voz.
VOICE_SYSTEM_SUFFIX = "ESTÁS EN MODO VOZ: tus respuestas se leen en voz alta, así que sé breve, natural y sin markdown."

_CONFIRM_TIMEOUT = 25  # s esperando el F2 de confirmación; si expira -> denegar (no colgar)

_YES = {
    "si",
    "sí",
    "claro",
    "dale",
    "autoriza",
    "autorizo",
    "adelante",
    "hazlo",
    "ok",
    "correcto",
    "afirmativo",
    "sip",
    "permito",
    "confirmo",
}
_NO = {"no", "cancela", "cancelar", "detente", "para", "negativo", "nel", "nop"}

# Categorías de permiso: agrupan tools afines para aprobarlas de una sola vez. La clave ES la
# frase hablada ("permiso para <valor>"), en lenguaje natural y sin símbolos.
_CATEGORIES: dict[str, str] = {
    "edit": "crear o modificar archivos en tu proyecto",
    "shell": "ejecutar comandos en la terminal",
    "memory": "guardar una nota en tu memoria",
    "orq": "poner en marcha una tarea del orquestador",
}


def _program(command: str) -> str:
    """Primer token 'limpio' de un comando (el ejecutable), para nombrarlo sin leer símbolos."""
    for token in str(command).strip().split():
        if re.fullmatch(r"[A-Za-z][\w.-]*", token):  # git, taskkill, python... no rutas ni flags
            return token
    return ""


def _describe(tool_name: str, input_data: dict[str, Any]) -> str:
    """Frase hablada, en lenguaje natural y SIN símbolos/rutas crudas, de la acción a confirmar."""
    cat = _category(tool_name)
    if cat == "shell":
        prog = _program(input_data.get("command", ""))
        return f"ejecutar en la terminal el comando {prog}" if prog else _CATEGORIES["shell"]
    if cat in _CATEGORIES:
        return _CATEGORIES[cat]
    return f"usar la herramienta {tool_name}"


def _short(data: dict[str, Any], limit: int = 200) -> str:
    """Serialización compacta del input crudo — solo para el ``print`` (log), nunca para el TTS."""
    try:
        text = json.dumps(data, ensure_ascii=False, default=str)
    except Exception:
        text = str(data)
    return text if len(text) <= limit else text[:limit] + "…"


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-záéíóúñ]+", (text or "").lower())


async def _ask_voice(accion: str) -> bool:
    """Pregunta hablada sí/no con timeout. True si autorizó; False si negó, no entendió o expiró.

    Toda la pregunta corre con el micrófono reservado (:func:`~agent.voice.ears.escucha_exclusiva`):
    esto pasa DENTRO de un turno del modelo, así que sin la reserva la F2 con la que el usuario
    contesta la vería también la guardia de barge-in del daemon y abortaría el turno que estaba
    pidiendo el permiso.
    """
    loop = asyncio.get_running_loop()
    verbo = config.voice_ptt_verbo()  # "mantén" (hold) / "presiona" (toggle)
    with ears.escucha_exclusiva():
        tts.speak(
            f"Necesito tu permiso para {accion}. {verbo.capitalize()} efe dos y dime sí o no."
        )
        try:
            answer = await loop.run_in_executor(
                None, lambda: ears.listen_once(wait_timeout=_CONFIRM_TIMEOUT)
            )
        except Exception:  # noqa: BLE001 - micrófono no disponible
            return False
        finally:
            # `listen_once` cierra su ciclo en `inactivo`, pero el turno sigue vivo: sin esto el
            # ícono se queda en azul mientras el modelo trabaja.
            ears.publicar_captura(ears.CAPTURA_PENSANDO)
    if answer is None:  # expiró el tiempo de espera sin pulsar F2
        tts.speak("No escuché tu respuesta, lo dejo sin autorizar.")
        return False
    toks = _tokens(answer)
    print(f"  respuesta de voz: {answer!r}")
    if any(t in _NO for t in toks):
        return False
    if any(t in _YES for t in toks):
        return True
    tts.speak("No te entendí, lo dejo sin autorizar.")
    return False


class VoiceGate:
    """Gate de permisos hablado con memoria por categoría (aprobar una vez, extender a la tarea).

    Se pasa como ``can_use_tool`` del SDK. La primera acción de cada categoría se confirma
    hablando; al aceptar, las siguientes de esa categoría se auto-aprueban dentro de la sesión —
    así el usuario no repite "sí" 12 veces para editar el mismo archivo. ``reset()`` al cambiar
    de tema vuelve a exigir confirmación. El gate duro (``guard``) sigue bloqueando lo destructivo
    aparte, sin importar esta aprobación.
    """

    def __init__(self) -> None:
        self.approved: set[str] = set()

    def reset(self) -> None:
        """Olvida las categorías aprobadas (nuevo tema / nueva tarea)."""
        self.approved.clear()

    async def __call__(
        self,
        tool_name: str,
        input_data: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        cat = _category(tool_name)
        if cat in self.approved:
            return PermissionResultAllow()  # ya autorizado en esta sesión: no re-preguntar
        print(f"\n[{config.AGENT_NAME} pide confirmación] {tool_name}  {_short(input_data)}")
        if await _ask_voice(_describe(tool_name, input_data)):
            self.approved.add(cat)  # extiende el permiso a las próximas de la misma categoría
            return PermissionResultAllow()
        return PermissionResultDeny(message="Rechazado por voz.")


async def confirm_action_voice(
    tool_name: str,
    input_data: dict[str, Any],
    context: ToolPermissionContext,
) -> PermissionResultAllow | PermissionResultDeny:
    """Confirmación hablada de un solo uso (sin memoria de categoría).

    Para invocaciones sueltas; el daemon residente usa :class:`VoiceGate` (aprobar-una-vez).
    """
    return await VoiceGate()(tool_name, input_data, context)
