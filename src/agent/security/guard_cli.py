"""Hook ``PreToolUse`` standalone para el dispatch headless (``claude -p``).

El loop conversacional del SDK ya pasa por :func:`guard.pretooluse_guard`; este
script cierra el otro carril (S1/H1): ``executors.run_agent`` lanza ``claude -p
--dangerously-skip-permissions`` como subproceso, donde el hook del SDK no existe.
``executors._guard_settings_path`` inyecta este archivo via ``--settings`` para que
el CLI lo ejecute antes de cada tool: lee el evento JSON por stdin, delega en
:func:`guard.evaluate` y, si hay razon de bloqueo, emite el deny JSON por stdout.

Fail-open a proposito: un error interno del hook NUNCA debe romper un dispatch
legitimo (exit 0 sin output = permitir); la regla dura vive en ``guard.evaluate``,
que es best-effort por diseno. Las mismas reglas, una sola fuente de verdad.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# El CLI ejecuta este archivo como SCRIPT suelto (no como modulo del paquete): anclar
# src/ al path para que ``agent.security.guard`` resuelva sin depender del cwd ni venv.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Debe coincidir con executors.DENY_SHELL_ENV (no se importa: executors arrastra config
# y este script debe seguir siendo stdlib-only, ejecutable por cualquier Python).
_DENY_SHELL_ENV = "AGENT_DENY_SHELL"


def main() -> int:
    try:
        data = json.load(sys.stdin)
        from agent.security import guard

        # S3/H3: run_agent(no_shell=True) marca el dispatch via env (el hook hereda el env
        # del CLI). En esos pasos de EDICIÓN pura, cualquier shell se deniega de plano.
        if os.environ.get(_DENY_SHELL_ENV) == "1" and data.get("tool_name") in (
            "Bash",
            "PowerShell",
        ):
            reason = "shell deshabilitado en este dispatch (paso de edición: no corre comandos)"
        else:
            reason = guard.evaluate(
                data.get("tool_name", ""),
                data.get("tool_input") or {},
                cwd=data.get("cwd"),
            )
        if reason:
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": f"[guard] {reason}",
                        }
                    }
                )
            )
    except Exception:
        pass  # fail-open: un bug del hook no debe tumbar un dispatch legitimo
    return 0


if __name__ == "__main__":
    sys.exit(main())
