"""Cableado del agente: construye ``ClaudeAgentOptions`` con el gate en capas.

Anillo 0 auto-aprobado (read-only), el resto tras confirmación, hook de seguridad siempre.
Lo usan tanto el REPL de texto (``agent.repl``) como el daemon de voz (``agent.voice.daemon``),
vía ``agent.cli.main``.
"""

from __future__ import annotations

from pathlib import Path

from typing import TYPE_CHECKING

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

from agent import config
from agent.security.guard import pretooluse_guard
from agent.security.permissions import TextGate
from agent.tools.memory import memory_server
from agent.tools.orchestrator import orchestrator_server
from agent.tools.semantic import semantic_server

if TYPE_CHECKING:
    from agent.voice.interaction import VoiceGate


def build_options(
    voice: bool = False, resume: str | None = None, gate: VoiceGate | None = None
) -> ClaudeAgentOptions:
    """Construye las opciones del agente con el gate en capas.

    Args:
        voice: en modo voz, la confirmación de acciones es hablada (sí/no por voz) y se
            desactiva ``AskUserQuestion`` para que el agente pregunte hablando.
        resume: ``session_id`` a reanudar (el daemon de voz retoma el hilo persistido);
            ``None`` = sesión nueva.
        gate: en modo voz, el :class:`VoiceGate` persistente que recuerda las categorías ya
            autorizadas (aprobar-una-vez). ``None`` cae a la confirmación de un solo uso.
    """
    system_prompt = (Path(__file__).parent / "prompts" / "system.md").read_text(encoding="utf-8")
    system_prompt = system_prompt.format(
        AGENT_NAME=config.AGENT_NAME,
        USER_NAME=config.USER_NAME,
        USER_PROFILE=config.USER_PROFILE,
    )
    # AskUserQuestion (menú interactivo) no encaja en el REPL ni en voz: que pregunte en su
    # respuesta, en texto, y el usuario contesta en el siguiente turno (no un s/N para preguntar).
    system_prompt += (
        "\n\nSi necesitas aclarar algo, NO uses la herramienta AskUserQuestion: formula la "
        "pregunta en tu respuesta, en lenguaje natural, y espera la respuesta en el siguiente turno."
    )
    disallowed = ["AskUserQuestion"]
    can_use = TextGate()  # texto: aprobar-una-vez por categoría (persistente en la sesión)
    if voice:
        from agent.voice.interaction import VOICE_SYSTEM_SUFFIX, confirm_action_voice

        system_prompt = f"{system_prompt}\n\n{VOICE_SYSTEM_SUFFIX}"
        can_use = gate if gate is not None else confirm_action_voice
    return ClaudeAgentOptions(
        model=config.MODEL_MAIN,
        system_prompt=system_prompt,
        mcp_servers={
            "memory": memory_server,
            "semantic": semantic_server,
            "orq": orchestrator_server,
        },
        allowed_tools=list(config.RING0_TOOLS),  # Anillo 0: read-only auto-aprobado
        disallowed_tools=disallowed,
        permission_mode="default",  # lo no listado cae a can_use_tool (confirmacion)
        can_use_tool=can_use,  # Anillos 1-3: confirmacion explicita (consola o voz)
        hooks={"PreToolUse": [HookMatcher(hooks=[pretooluse_guard])]},  # gate duro
        setting_sources=[],  # entorno limpio: permisos 100% programaticos, sin heredar settings
        # Deltas del stream: solo en voz, y solo si el habla en streaming está activa (el daemon
        # los usa para hablar por frases mientras el modelo escribe). El REPL de texto no los
        # necesita: imprime el mensaje completo.
        include_partial_messages=voice and config.VOICE_STREAM_TTS,
        resume=resume,
        cwd=str(config.HOME),
    )
