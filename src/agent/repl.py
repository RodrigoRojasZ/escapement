"""REPL de texto del agente: lee una línea, la enruta (local/Claude) y muestra la respuesta.

Interfaz hermana del daemon de voz (``agent.voice.daemon``); ambas reciben las ``options`` que
arma ``agent.session.build_options``.
"""

from __future__ import annotations

import asyncio

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLINotFoundError,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from agent import config, convlog
from agent.router import ask_local, route

_EXIT = {"salir", "exit", "quit", ":q"}


def _render(message: object) -> str:
    """Imprime el texto y los indicadores de tool de un mensaje; devuelve su texto (para el log)."""
    parts: list[str] = []
    if isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, TextBlock):
                print(block.text)
                parts.append(block.text)
            elif isinstance(block, ToolUseBlock):
                print(f"  · {block.name}")
    return " ".join(parts)


async def run_repl(options: ClaudeAgentOptions) -> None:
    """REPL de texto: lee una linea, la manda al agente, imprime la respuesta."""
    loop = asyncio.get_running_loop()
    print(f"{config.AGENT_NAME} listo. Escribe 'salir' para terminar.")
    try:
        async with ClaudeSDKClient(options=options) as client:
            last_backend: str | None = None
            while True:
                try:
                    user = (await loop.run_in_executor(None, lambda: input("\ntú › "))).strip()
                except (EOFError, KeyboardInterrupt):
                    print("\nHasta luego.")
                    return
                if not user:
                    continue
                if user.lower() in _EXIT:
                    print("Hasta luego.")
                    return
                if route(user, last_backend) == "local":  # manejable -> LLM local, sin egress
                    reply = await loop.run_in_executor(None, ask_local, user)
                    print(f"  → [local]\n{reply}")
                    convlog.record(user, reply, backend="local")
                    last_backend = "local"
                    continue
                await client.query(user)
                parts: list[str] = []
                session_id: str | None = None
                async for message in client.receive_response():
                    if isinstance(message, ResultMessage):
                        session_id = message.session_id
                        continue
                    parts.append(_render(message))
                convlog.record(
                    user,
                    " ".join(p for p in parts if p).strip(),
                    backend="claude",
                    session_id=session_id,
                )
                last_backend = "claude"
    except CLINotFoundError:
        print(
            "No encuentro el runtime de Claude. Verifica tu autenticacion "
            "(ANTHROPIC_API_KEY en .env, o inicia sesion con Claude Code)."
        )
