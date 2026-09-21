"""Detección de secretos hardcodeados (P1: no despachar código con secretos a la API del executor).

Antes de despachar un refactor, el orquestador escanea el archivo objetivo. Si contiene secretos
LITERALES (no referencias a env vars), aborta: enviar ese código al motor sería un egress de
credenciales. Conservador —mejor un falso positivo (aborta; se fuerza con ``allow_secrets``) que
filtrar un secreto—, pero NO marca ``os.getenv("X_PASSWORD")`` ni ``config.PASSWORD`` (referencias,
no valores) ni placeholders obvios. El worktree ya excluye archivos gitignored (``.env``); esto
cubre los secretos VERSIONADOS en el propio código.
"""

from __future__ import annotations

import re

# Cada patrón captura un secreto LITERAL. Si tiene grupo 1, ese es el valor a redactar.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Connection string con credenciales embebidas: scheme://user:pass@host
    ("connection-string", re.compile(r"\b\w+://[^:/\s]+:([^@/\s]{3,})@", re.I)),
    # Clave privada PEM
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |)?PRIVATE KEY-----")),
    # AWS access key id
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # Asignación de string literal a una variable de secreto (excluye os.getenv/environ: el valor
    # tras =/: debe ser una comilla, no una llamada).
    (
        "hardcoded-secret",
        re.compile(
            r"\b(?:password|passwd|pwd|secret|api[_-]?key|apikey|access[_-]?key|auth[_-]?token"
            r"|client[_-]?secret|private[_-]?key)\b\s*[=:]\s*[\"']([^\"'\s]{6,})[\"']",
            re.I,
        ),
    ),
    # Tokens con prefijo reconocible (OpenAI, GitHub, Slack)
    (
        "api-token",
        re.compile(r"\b(sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|xox[bp]-[A-Za-z0-9-]{10,})"),
    ),
)

# Valores obviamente-placeholder que NO son secretos reales (reducen falsos positivos).
_PLACEHOLDER = re.compile(
    r"^(?:changeme|password|secret|x{3,}|your[_-]?\w+|example|placeholder|none|null|test|dummy"
    r"|\*{3,}|<[^>]+>|\{[^}]+\})$",
    re.I,
)


def _redact(value: str) -> str:
    v = value.strip()
    return f"{v[:3]}…{v[-2:]}" if len(v) > 8 else "***"


def find_secrets(text: str) -> list[str]:
    """Devuelve los secretos LITERALES detectados, redactados (``[]`` = limpio)."""
    hits: list[str] = []
    for name, pat in _PATTERNS:
        for m in pat.finditer(text or ""):
            value = m.group(1) if m.groups() else m.group(0)
            if _PLACEHOLDER.match(value.strip()):
                continue
            hits.append(f"{name}: {_redact(value)}")
    return hits
