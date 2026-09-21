"""El gate: la unica puerta dura del agente.

Portado de ``~/.claude/hooks/guard.py`` al formato de hook ``PreToolUse`` del
Agent SDK. La decision vive en :func:`evaluate` (pura, testeable sin SDK ni red);
:func:`pretooluse_guard` la envuelve al shape que el SDK espera.

Reglas (heredadas de la CLAUDE.md global del usuario + endurecidas):
  1. No ejecutar SQL destructivo (UPDATE/INSERT/DELETE/ALTER/GRANT/DROP/TRUNCATE),
     solo cuando hay indicador de EJECUCION (cursor/execute/mysql/...), para no
     falsear en un simple ``grep "DELETE FROM"``.
  2. No ``git commit``/``push`` sobre rama protegida (``main``/``master`` mas las que
     declare la config; ver :data:`PROTECTED`).
  3. No leer ni escribir archivos ``.env`` (secretos).

El hook corre ANTES que las allow-rules, asi que deniega incluso tools que estan
en ``allowed_tools`` (p.ej. ``Read`` del Anillo 0 intentando abrir un ``.env``).
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Any

from agent import config

# Ramas donde el gate prohibe commit/push. Sale de config para que cada instalacion sume
# la suya (su rama de integracion, git-flow, ...). La config solo ANADE, nunca quita:
# ``main``/``master`` no se desprotegen desde un archivo ni desde una env var, porque un
# gate que se apaga por config no es un gate. Ver config.PROTECTED_BRANCHES.
PROTECTED = set(config.PROTECTED_BRANCHES)

# SQL destructivo como FORMAS DE SENTENCIA (no keywords sueltas) -> pocos falsos positivos.
_SQL = re.compile(
    r"\b(delete\s+from|update\s+[`\"\[]?\w+[`\"\]]?\s+set|insert\s+into|replace\s+into"
    r"|truncate\s+(table\s+)?\w|alter\s+table|drop\s+(table|database|schema|index)"
    r"|grant\s+.+?\bon\b)\b",
    re.IGNORECASE | re.DOTALL,
)
# Solo bloquear SQL cuando de verdad se EJECUTA (la regla es sobre *ejecutar*).
_EXEC = re.compile(
    r"(\.execute|executemany|\bcursor\b|\bmysql\b|\bpsql\b|sqlalchemy|create_engine"
    r"|mysql\.connector|\bpymysql\b|\bconn\b|connection\.|\bengine\.)",
    re.IGNORECASE,
)
_GIT_COMMIT = re.compile(r"\bgit\b[^|&;\n]*\bcommit\b", re.IGNORECASE)
_GIT_PUSH = re.compile(r"\bgit\b[^|&;\n]*\bpush\b", re.IGNORECASE)
def _protected_word() -> re.Pattern[str]:
    """Regex de un nombre de rama protegida escrito EN el comando (``... origin <rama>``).

    Se deriva de :data:`PROTECTED` en CADA llamada, no al importar: asi un ajuste del set
    (un test, otra config) no puede dejar el regex decidiendo por un set viejo.
    ``re.compile`` cachea el patron, asi que el costo es una busqueda en dict.

    Returns:
        El patron compilado. Con ``PROTECTED`` vacio devuelve uno que NUNCA matchea
        (``(?!)``), nunca uno vacio -- que matchearia cualquier comando.
    """
    alternativas = "|".join(re.escape(b) for b in sorted(PROTECTED)) or "(?!)"
    return re.compile(rf"\b({alternativas})\b", re.IGNORECASE)
# Sinks que leen/muestran/copian/cargan el contenido de un archivo: pagers y editores,
# dumpers binarios, filtros de texto, transferencia (curl file://, scp), interpretes
# (python -c "open('.env')") y cmdlets de PowerShell. La condicion exige ademas una
# referencia a .env (_ENV_REF), asi que la amplitud no genera falsos positivos fuera de eso.
_READ_CMD = re.compile(
    r"\b(cat|type|less|more|head|tail|nano|vim|vi|code|strings|od|xxd|hexdump|base64|dd"
    r"|grep|egrep|fgrep|rg|findstr|awk|sed|cut|sort|uniq|tee|source"
    r"|curl|wget|scp|certutil|python[w\d.]*|py|node|ruby|perl|php|pwsh|powershell"
    r"|Get-Content|gc|Get-Item|gi|Select-String|sls|Invoke-WebRequest|iwr"
    r"|Invoke-RestMethod|irm|Copy-Item|copy|cp)\b",
    re.IGNORECASE,
)
# Plantillas commiteadas NO secretas (.env.example y familia): leerlas/copiarlas es el
# bootstrap normal de un repo, no una fuga. (.env.local SI es secreto: valores reales.)
_ENV_TEMPLATE = r"example|sample|template|dist"
# Referencia a un archivo .env / .env.<algo> dentro de un comando de shell (plantillas excluidas).
_ENV_REF = re.compile(rf"(^|[\s='\"/\\])\.env(?!\.(?:{_ENV_TEMPLATE})\b)(\.[\w-]+)?(\b|['\"\s]|$)")
# Redireccion de entrada directa desde un .env (p.ej. ``mysql < .env``): lee sin comando lector.
_REDIR_ENV = re.compile(
    rf"<\s*['\"]?[^\s|&;<>]*\.env(?!\.(?:{_ENV_TEMPLATE})\b)(\.[\w-]+)?['\"]?(\s|$)"
)
# Bootstrap canonico: copiar una PLANTILLA a .env crea el archivo local, no lee ningun secreto.
_ENV_BOOTSTRAP = re.compile(
    rf"^\s*(cp|copy|Copy-Item)\s+(-\S+\s+)*['\"]?\S*\.env\.(?:{_ENV_TEMPLATE})['\"]?\s+"
    r"['\"]?\S*\.env['\"]?\s*$",
    re.IGNORECASE,
)

# Ejecucion de un script .py (python/py/uv run): el SQL destructivo puede vivir DENTRO del
# script, no en la linea de comando — hay que inspeccionar el archivo referenciado.
_PY_RUN = re.compile(
    r"(?:\b(?:python[w\d.]*|py)(?:\.exe)?|\buv\s+run)\s+(?:-\S+\s+)*"
    r"(\"[^\"]+\.py\"|'[^']+\.py'|[^\s;|&<>'\"]+\.py)(?=[\s;|&<>)'\"]|$)",
    re.IGNORECASE,
)
_MAX_SCRIPT_BYTES = 2_000_000  # tope de lectura: no cargar archivos gigantes en el hook

_AUTO = "__auto__"


_ENV_TEMPLATE_FILE = re.compile(rf"^\.env\.(?:{_ENV_TEMPLATE})\b", re.IGNORECASE)


def is_env_path(path: str) -> bool:
    """True si ``path`` apunta a un ``.env`` o ``.env.<algo>`` (secreto).

    Las plantillas commiteadas (``.env.example``/``sample``/``template``/``dist``) no cuentan:
    no contienen valores reales y leerlas es parte del setup normal de cualquier repo.
    """
    if not path:
        return False
    base = os.path.basename(path.replace("\\", "/").rstrip("/"))
    if base == ".env":
        return True
    return base.startswith(".env.") and not _ENV_TEMPLATE_FILE.match(base)


def current_branch(cwd: str | None = None) -> str | None:
    """Rama git actual en ``cwd`` (o el cwd del proceso). None si no aplica."""
    try:
        out = subprocess.run(
            ["git", "-C", cwd or os.getcwd(), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


def _tool_path(tool_input: dict[str, Any]) -> str:
    for k in ("file_path", "path", "notebook_path"):
        v = tool_input.get(k)
        if v:
            return str(v)
    return ""


def _script_sql(cmd: str, cwd: str | None) -> bool:
    """True si ``cmd`` ejecuta un script ``.py`` cuyo contenido tiene SQL destructivo en ejecucion.

    Cubre el hueco de la evaluacion por linea de comando: ``python borra.py`` no muestra el SQL
    en ``cmd``, pero el script si lo contiene. Best-effort: si el archivo no existe o no se puede
    leer, no bloquea (el guard nunca debe romper un comando legitimo por un error propio).
    """
    for m in _PY_RUN.finditer(cmd):
        raw = m.group(1).strip("'\"")
        path = raw if os.path.isabs(raw) else os.path.join(cwd or os.getcwd(), raw)
        try:
            if os.path.isfile(path) and os.path.getsize(path) <= _MAX_SCRIPT_BYTES:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                if _SQL.search(text) and _EXEC.search(text):
                    return True
        except OSError:
            continue
    return False


def evaluate(
    tool_name: str,
    tool_input: dict[str, Any] | None,
    *,
    cwd: str | None = None,
    branch: str | None = _AUTO,
) -> str | None:
    """Evalua una llamada a tool. Devuelve la razon de bloqueo, o None si se permite.

    Args:
        tool_name: nombre de la tool (Bash, Read, Write, ...).
        tool_input: parametros de la tool.
        cwd: directorio para resolver la rama git (default: cwd del proceso).
        branch: rama git a usar; ``"__auto__"`` la detecta via git. Pasar una
            cadena explicita (o ``None``) en tests para no invocar git.
    """
    ti = tool_input or {}

    # PowerShell es la MISMA superficie que Bash (el gate ya los agrupa como "shell"):
    # sin esto, cualquier regla se bypassea con solo cambiar de tool.
    if tool_name in ("Bash", "PowerShell"):
        cmd = str(ti.get("command") or "")
        if not cmd.strip():
            return None
        if (_SQL.search(cmd) and _EXEC.search(cmd)) or _script_sql(cmd, cwd):
            return (
                "SQL destructivo en ejecucion (UPDATE/INSERT/DELETE/ALTER/GRANT/DROP/"
                "TRUNCATE). Regla global: prohibido ejecutar SQL destructivo, incluido "
                "via python. Si es intencional, hazlo tu manualmente fuera del agente."
            )
        if _GIT_COMMIT.search(cmd) or _GIT_PUSH.search(cmd):
            is_push = bool(_GIT_PUSH.search(cmd))
            explicit = bool(is_push and _protected_word().search(cmd))
            br = current_branch(cwd) if branch == _AUTO else branch
            if explicit or (br in PROTECTED):
                target = br if br in PROTECTED else "/".join(sorted(PROTECTED))
                action = "push" if is_push else "commit"
                return (
                    f"git {action} sobre rama protegida ({target}). Regla global: crea "
                    "una rama feature primero (git checkout -b <nombre>)."
                )
        if (
            _ENV_REF.search(cmd)
            and not _ENV_BOOTSTRAP.match(cmd)
            and (_READ_CMD.search(cmd) or _REDIR_ENV.search(cmd))
        ):
            return "Lectura de .env bloqueada (secreto)."
        return None

    if tool_name in ("Read", "Grep", "Glob", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        if is_env_path(_tool_path(ti)):
            return "Acceso a archivo .env bloqueado (secreto)."

    return None


async def pretooluse_guard(
    input_data: dict[str, Any],
    tool_use_id: str | None,
    context: Any,
) -> dict[str, Any]:
    """Hook ``PreToolUse``: deniega denegando; permite devolviendo ``{}``."""
    if input_data.get("hook_event_name") != "PreToolUse":
        return {}
    reason = evaluate(
        input_data.get("tool_name", ""),
        input_data.get("tool_input") or {},
        cwd=input_data.get("cwd"),
    )
    if reason:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"[guard] {reason}",
            }
        }
    return {}
