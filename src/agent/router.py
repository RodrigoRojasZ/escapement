"""Router híbrido (F1): decide el backend de cada turno.

- Manejable (chat/conocimiento simple, sin tools ni datos frescos) -> LLM local
  (Ollama, modelo configurable ``config.LOCAL_LLM_MODEL``), sin egress.
- Compleja, con tools/acciones/datos-actuales, razonamiento sustancial o turno largo -> Claude.
- Override explícito del usuario ("online"/"local").

Fallback seguro: en duda (turno largo o señales de razonamiento) -> 'claude'; y si el LLM
local no responde, SIEMPRE 'claude'. El local es opt-in por confianza, no el cajón de sastre.
"""

from __future__ import annotations

import re
from functools import lru_cache

from agent import config

# Overrides explícitos del usuario.
_FORCE_ONLINE = re.compile(
    r"\b(online|en la nube|con claude|usa claude|razonamiento (pesado|profundo)|piensa (bien|profundo|a fondo))\b",
    re.IGNORECASE,
)
_FORCE_LOCAL = re.compile(
    r"\b(local|offline|sin internet|sin conexión|en local|usa qwen|modo local)\b", re.IGNORECASE
)
# Señales de que la tarea necesita el andamiaje de Claude (tools, acciones, datos frescos).
_NEEDS_CLAUDE = re.compile(
    r"\b(lanza|ejecuta|corre|abre|cierra|instala|edita|escribe|borra|elimina|mata|mueve|"
    r"scraper|chrome|powershell|terminal|comando|archivo|carpeta|git|commit|"
    r"recuerda|guarda en memoria|recall|busca en (mi|la) memoria|"
    r"web|internet|clima|tiempo|noticias|precio|cotización|hoy|actual|último|reciente)\b",
    re.IGNORECASE,
)
# Señales de razonamiento sustancial: aunque no toquen tools, el LLM local rinde pobre -> Claude.
_NEEDS_REASONING = re.compile(
    r"\b(analiza|analizar|compara|comparar|diseña|diseñar|evalúa|evaluar|explica por qué|"
    r"por qué|estrategia|arquitectura|refactor|depura|debuggea|optimiza|pros y contras|"
    r"ventajas y desventajas|planifica|resuelve|demuestra|razona|diagnostica)\b",
    re.IGNORECASE,
)
_LONG_TURN_WORDS = 40  # turnos largos = tarea sustancial; en duda -> online (calidad > ahorro)

# Follow-ups de continuación: turnos cortos que dependen del turno anterior ("prosigue", "sí").
_FOLLOWUP = re.compile(
    r"^(s[ií]|ok|okay|dale|adelante|prosigue|contin[úu]a|sigue|hazlo|hazlo ya|"
    r"listo|perfecto|de acuerdo|correcto|exacto|va|ya|más|mas|también|tambien|luego)\b",
    re.IGNORECASE,
)
# Clasificador de respaldo: el propio LLM decide acción vs charla cuando el heurístico duda.
_CLASSIFY_SYSTEM = (
    "Eres un clasificador de intención. Responde SOLO una palabra: accion o charla. "
    "CHARLA: se responde hablando, con conocimiento o información, SIN herramientas — saludos, "
    "conversación, conocimiento general, y preguntas SOBRE TI MISMO (tu nombre, tus rutas, tu "
    "configuración, qué puedes hacer). "
    "ACCION: requiere herramientas o efectos — leer/editar/crear archivos o código, ejecutar u "
    "operar algo, buscar datos actuales en internet, tareas de ingeniería, o análisis serio. "
    "Si dudas entre ambas, responde accion."
)


def _is_thinking_model(model: str) -> bool:
    """True si el modelo local razona con bloques ``<think>`` (familia qwen3) y admite ``/no_think``."""
    return "qwen3" in (model or "").lower()


def _no_think_suffix(model: str | None = None) -> str:
    """`` /no_think`` si el modelo es de razonamiento; ``""`` si no (qwen2.5 no entiende el token).

    Se anexa al system prompt SOLO para la familia qwen3: en qwen2.5 el literal ``/no_think`` quedaría
    como texto basura en el prompt. ``model=None`` consulta el modelo local configurado por defecto.
    """
    return " /no_think" if _is_thinking_model(model or config.LOCAL_LLM_MODEL) else ""


def build_system_local() -> str:
    return (
        f"Eres {config.AGENT_NAME}, el asistente personal de {config.USER_NAME}. Responde en español latino neutro, "
        "breve y directo, sin markdown. Prioriza recomendaciones útiles, accionables y verificables, "
        "y adapta el nivel de detalle al contexto de la tarea. "
        f"Datos ciertos sobre ti (úsalos para responder sobre ti mismo, sin inventar): tu código —la "
        f"aplicación de IA— vive en '{config.REPO_ROOT}'; tus notas de memoria en '{config.AGENT_HOME}' "
        f"y tus datos (ledger, cola, conversaciones) en '{config.DATA_DIR}'; "
        f"tu modelo local es '{config.LOCAL_LLM_MODEL}' sobre Ollama, y para tareas complejas delegas en Claude. "
        "Si la petición requiere herramientas, acciones o datos actuales de internet, dilo claramente y "
        "sugiere resolverlo en modo online. Cuando no tengas suficiente contexto, pide una aclaración breve. "
        "No inventes hechos ni datos; si no lo sabes, dilo con honestidad." + _no_think_suffix()
    )


class _DynamicSystemLocal:
    def __str__(self) -> str:
        return build_system_local()

    def __repr__(self) -> str:
        return self.__str__()

    def __contains__(self, item: object) -> bool:
        return str(item) in self.__str__()

    def lower(self) -> str:
        return self.__str__().lower()

    def __bool__(self) -> bool:
        return bool(self.__str__())


SYSTEM_LOCAL = _DynamicSystemLocal()


@lru_cache(maxsize=1)
def _available() -> bool:
    """True si el endpoint de Ollama responde (se verifica una vez por sesión)."""
    try:
        import httpx

        url = config.LOCAL_LLM_BASE_URL.replace("/v1", "/api/tags")
        return httpx.get(url, timeout=2.0).status_code == 200
    except Exception:
        return False


def _classify(user_text: str) -> str:
    """Clasifica el turno asumiendo el LLM local disponible: ``"local"`` o ``"claude"``.

    Separada de ``route`` para testear la heurística sin depender de Ollama.
    """
    text = (user_text or "").strip()
    if _FORCE_ONLINE.search(text):
        return "claude"
    if _FORCE_LOCAL.search(text):
        return "local"
    if _NEEDS_CLAUDE.search(text) or _NEEDS_REASONING.search(text):
        return "claude"
    if len(text.split()) > _LONG_TURN_WORDS:
        return "claude"  # en duda (turno largo/sustancial) -> online, prioriza calidad
    return "local"  # solo lo corto y sin señales -> local


def _is_followup(text: str) -> bool:
    """True si el turno es una continuación corta ('sí', 'prosigue') que depende del anterior."""
    t = (text or "").strip()
    return bool(_FOLLOWUP.match(t)) or len(t.split()) <= 2


def _classify_llm(user_text: str) -> str:
    """Respaldo: el propio LLM local decide acción (-> 'claude') vs charla (-> 'local').

    Cubre los falsos negativos del heurístico de regex (conjugaciones, vocabulario nuevo).
    En duda o sin LLM -> 'claude': mejor sobre-escalar que paralizarse en el local sin tools.
    """
    try:
        resp = ask_local(user_text, system_prompt=_CLASSIFY_SYSTEM + _no_think_suffix())
    except Exception:
        return "claude"
    return "local" if "charla" in resp.lower() else "claude"


def route(user_text: str, last_backend: str | None = None) -> str:
    """Devuelve el backend del turno: ``"local"`` o ``"claude"``.

    Args:
        last_backend: backend del turno anterior. Un follow-up corto ('prosigue', 'sí') tras un
            turno de Claude se queda en Claude —que conserva el estado y las tools—, evitando que
            la continuación rebote al local (stateless) y pierda el hilo. ``None`` desactiva el
            sticky (primer turno o llamada suelta).
    """
    if not (config.LOCAL_LLM_ENABLED and _available()):
        return "claude"
    text = (user_text or "").strip()
    if _FORCE_ONLINE.search(text):
        return "claude"
    if _FORCE_LOCAL.search(text):
        return "local"
    if last_backend == "claude" and _is_followup(text):
        return "claude"  # continuidad: sigue el hilo con estado y tools
    if _classify(text) == "claude":
        return "claude"  # señales positivas claras -> online
    # El heurístico dice 'local', pero la regex es frágil: que el LLM confirme acción vs charla.
    return _classify_llm(text)


def _strip_think(text: str) -> str:
    """Quita los bloques de razonamiento ``<think>...</think>`` de qwen3."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def ask_local(user_text: str, system_prompt: str | None = None) -> str:
    """Consulta el LLM local (Ollama) y devuelve la respuesta limpia."""
    from openai import OpenAI

    client = OpenAI(base_url=config.LOCAL_LLM_BASE_URL, api_key="ollama")
    system_prompt = system_prompt or build_system_local()
    resp = client.chat.completions.create(
        model=config.LOCAL_LLM_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        extra_body={"keep_alive": config.LOCAL_LLM_KEEP_ALIVE},
    )
    return _strip_think(resp.choices[0].message.content or "")
