"""Executors pluggables: el motor que aplica los refactors y juzga los diffs.

Por defecto Claude Code (``claude -p``). Configurable con la env var ``AGENT_EXECUTOR``
(``claude`` | ``antigravity`` | ``cursor``) para que quien no use Claude aproveche el mismo
orquestador: el pipeline (worktree, verify, eval, PR, cola) es agnóstico del motor; sólo cambia
esta capa.

El system prompt lo antepone quien llama (``dispatch``/``judge``) al prompt — así no dependemos
de un flag propietario tipo ``--append-system-prompt``. Los flags de auto-aprobación dan permisos
totales en el CWD; por eso el orquestador SIEMPRE corre en un worktree aislado (el "sandbox" que
cada CLI recomienda). Verifica los flags contra ``<bin> --help`` de tu versión: ajustarlos aquí
es trivial (son builders por motor).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agent import config, fsutil

# Un builder recibe (bin, prompt) y devuelve (argv, stdin_text): stdin_text es el prompt por
# stdin, o None si el prompt ya va embebido en argv como argumento. Los builders de EDICIÓN
# aceptan además ``no_shell`` keyword (S3): confinar el dispatch a tools sin shell.
Builder = Callable[[str, str], "tuple[list[str], str | None]"]

# Canal interno run_agent -> guard_cli (S3): el hook hereda el env del CLI, así que esta var
# le dice "este dispatch NO debe usar shell" con la garantía de los hooks (que SÍ corren bajo
# --dangerously-skip-permissions), sin depender de la semántica de --disallowedTools.
DENY_SHELL_ENV = "AGENT_DENY_SHELL"

# Observer opcional de instrumentación (R3): si está seteado, run_agent lo llama con
# (mode, prompt, output) tras CADA dispatch completado —el sink único al executor—. Default None =
# no-op (comportamiento previo intacto). Lo usa costs.track para contabilizar tokens estimados por
# plan sin tocar la firma de run_agent ni de los handlers.
_observer: "Callable[[str, str, str], None] | None" = None


def set_dispatch_observer(
    fn: "Callable[[str, str, str], None] | None",
) -> "Callable[[str, str, str], None] | None":
    """Instala el observer de dispatch y devuelve el previo (para restaurarlo). None = lo quita."""
    global _observer
    prev = _observer
    _observer = fn
    return prev


def notify_observer(mode: str, prompt: str, output: str) -> None:
    """Notifica al observer de dispatch para trabajo que NO pasa por :func:`run_agent`.

    Lo usan los backends in-process (p.ej. :func:`agent.local_models.complete`) para contabilizar
    sus tokens estimados en el mismo ledger de costos. Mismo contrato blindado que el sink de
    ``run_agent`` (`:220`): jamás propaga una excepción del observer —la instrumentación no debe
    tumbar un dispatch—. No-op si no hay observer instalado.

    Args:
        mode: etiqueta del dispatch para el ruteo de costos (``"local"`` para el LLM local).
        prompt: texto enviado al modelo (base de la estimación de tokens de entrada).
        output: texto recibido (base de la estimación de tokens de salida).
    """
    if _observer is None:
        return
    try:
        _observer(mode, prompt, output)
    except Exception:  # noqa: BLE001 - la instrumentación jamás debe tumbar un dispatch
        pass


def _model_flag(model: str = "") -> list[str]:
    # --model <id> del tiering por tarea (config.model_for) o, si no, el override global
    # AGENT_EXECUTOR_MODEL. Ambos vacíos = default de Claude Code (no-op, comportamiento previo).
    m = model or config.EXECUTOR_MODEL
    return ["--model", m] if m else []


def _guard_settings_path() -> str:
    """Escribe (idempotente) el settings que inyecta el guard al ``claude -p`` y da su ruta.

    Cierra el bypass del dispatch headless (S1/H1): ``--dangerously-skip-permissions``
    salta los prompts de permiso pero NO los hooks, así que ``--settings`` con este
    archivo hace que el CLI ejecute ``guard_cli.py`` antes de cada tool y respete su
    deny — las mismas reglas de ``guard.evaluate`` que ya rigen el loop conversacional.
    Solo claude tiene mecanismo de hooks; agy/cursor quedan sin este gate (limitación
    documentada): su única protección sigue siendo el worktree aislado + pre-flight.
    """
    cli = Path(__file__).resolve().parent / "security" / "guard_cli.py"
    settings = {
        "hooks": {
            "PreToolUse": [
                {"hooks": [{"type": "command", "command": f'"{sys.executable}" "{cli}"'}]}
            ]
        }
    }
    path = config.DATA_DIR / "guard_settings.json"
    fsutil.write_text_atomic(path, json.dumps(settings, indent=2))
    return str(path)


def _claude_edit(
    binp: str, prompt: str, no_shell: bool = False, model: str = ""
) -> tuple[list[str], str | None]:
    # print mode + permisos de edición; prompt por stdin (evita el truncado del arg multi-línea).
    # --settings inyecta el hook PreToolUse del guard (ver _guard_settings_path).
    # no_shell (S3): capa declarativa; la garantía real es el hook vía DENY_SHELL_ENV.
    return [
        binp,
        "-p",
        "--dangerously-skip-permissions",
        "--settings",
        _guard_settings_path(),
        *_model_flag(model),
        *(["--disallowedTools", "Bash", "PowerShell"] if no_shell else []),
    ], prompt


def _claude_read(binp: str, prompt: str, model: str = "") -> tuple[list[str], str | None]:
    return [
        binp,
        "-p",
        *_model_flag(model),
    ], prompt  # sólo responde, sin permisos de edición (juez)


def _agy_edit(
    binp: str, prompt: str, no_shell: bool = False, model: str = ""
) -> tuple[list[str], str | None]:
    # agy -p "<prompt>" --headless --approve all: auto-aprueba writes (seguro en el worktree).
    # no_shell/model se ignoran: agy no tiene deny de tools ni selección de modelo (ver run_agent).
    return [binp, "-p", prompt, "--headless", "--approve", "all"], None


def _agy_read(binp: str, prompt: str, model: str = "") -> tuple[list[str], str | None]:
    return [binp, "-p", prompt, "--headless"], None  # sin --approve: sólo responde


def _cursor_edit(
    binp: str, prompt: str, no_shell: bool = False, model: str = ""
) -> tuple[list[str], str | None]:
    # no_shell/model se ignoran: cursor no tiene deny de tools ni selección de modelo (ver run_agent).
    return [binp, "-p", prompt, "--force", "--output-format", "text"], None


def _cursor_read(binp: str, prompt: str, model: str = "") -> tuple[list[str], str | None]:
    return [binp, "-p", prompt, "--output-format", "text"], None


@dataclass(frozen=True)
class Executor:
    name: str
    bin_name: str
    build_edit: Builder  # comando para APLICAR cambios (el refactor)
    build_read: Builder  # comando para SÓLO responder (el juez)


EXECUTORS: dict[str, Executor] = {
    "claude": Executor("claude", "claude", _claude_edit, _claude_read),
    "antigravity": Executor("antigravity", "agy", _agy_edit, _agy_read),
    "cursor": Executor("cursor", "cursor-agent", _cursor_edit, _cursor_read),
}


def resolve(name: str | None = None) -> Executor:
    """Devuelve el Executor por nombre (o ``config.EXECUTOR``). ValueError claro si no existe."""
    key = (name or config.EXECUTOR or "claude").lower()
    if key not in EXECUTORS:
        raise ValueError(f"AGENT_EXECUTOR desconocido: {key!r}. Válidos: {sorted(EXECUTORS)}")
    return EXECUTORS[key]


def run_agent(
    prompt: str,
    cwd: object,
    timeout: float = config.EXECUTOR_TIMEOUT,
    mode: str = "edit",
    executor: Executor | None = None,
    allow_secrets: bool = False,
    no_shell: bool = False,
    model: str = "",
) -> tuple[bool, str]:
    """Corre el executor con ``prompt`` en ``cwd``. Devuelve ``(ok, salida)``.

    Args:
        cwd: directorio de trabajo (worktree aislado para editar; tmp para el juez).
        timeout: techo de tiempo de ESTE dispatch, en segundos. Default
            ``config.EXECUTOR_TIMEOUT`` (env ``AGENT_EXECUTOR_TIMEOUT``, 5400 s): pasarlo
            explícito solo tiene sentido para un dispatch corto y acotado.
        mode: ``"edit"`` (aplica cambios, el refactor) o ``"read"`` (sólo responde, el juez).
        executor: fuerza un ``Executor``; si es None, usa ``config.EXECUTOR``.
        allow_secrets: fuerza el dispatch aunque el prompt contenga secretos hardcodeados.
            Por defecto False: en modo ``edit`` TODO prompt pasa el pre-flight de secretos
            aquí (la única puerta al executor), no solo el carril ``optimize``.
        no_shell: confina el dispatch a tools sin shell (S3/H3): para pasos de EDICIÓN pura,
            donde el prompt puede venir de input no confiable (planner/reflexión) y correr
            comandos no es parte de la tarea. Default False (no-op): ``ejecutar``/``verificar``
            necesitan shell y siguen cubiertos por el guard (S1) + worktree (S2). Con claude
            se aplica por dos capas (env ``AGENT_DENY_SHELL`` que honra el hook del guard, y
            ``--disallowedTools``); agy/cursor no tienen mecanismo equivalente y lo ignoran.
        model: id de modelo para este dispatch (tiering por tarea, ``config.model_for``). ``""``
            (default) = sin ``--model`` propio → cae al override global ``AGENT_EXECUTOR_MODEL``
            y, si tampoco, al default del CLI (comportamiento previo). Solo claude lo aplica;
            agy/cursor lo ignoran (no exponen selección de modelo por flag).
    """
    # Pre-flight de secretos en el SINK: cualquier ruta (runner, orquestador, futura) que
    # despache código a la API del executor pasa por aquí, sin depender de que quien llama
    # se acuerde de escanear. En modo "read" no aplica (el juez no recibe permisos de edición
    # y su prompt lo arma el orquestador tras este mismo filtro).
    if mode == "edit" and not allow_secrets:
        from agent.security.secrets import find_secrets

        secretos = find_secrets(prompt)
        if secretos:
            return False, (
                "pre-flight de seguridad: el prompt contiene secretos, no se despacha a la "
                f"API (allow_secrets=True para forzar): {'; '.join(secretos[:5])}"
            )
    ex = executor or resolve()
    binp = shutil.which(ex.bin_name) or ex.bin_name
    if mode == "edit":
        cmd, stdin = ex.build_edit(binp, prompt, no_shell=no_shell, model=model)
    else:
        cmd, stdin = ex.build_read(binp, prompt, model=model)
    try:
        r = subprocess.run(
            cmd,
            input=stdin,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={**os.environ, DENY_SHELL_ENV: "1"} if no_shell else None,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"executor {ex.name} falló: {exc}"
    ok, out = r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    if _observer is not None:
        try:
            _observer(mode, prompt, out)
        except Exception:  # noqa: BLE001 - la instrumentación jamás debe tumbar un dispatch
            pass
    return ok, out
