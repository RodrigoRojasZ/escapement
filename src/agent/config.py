"""Configuracion central de Escapement.

Un solo lugar para rutas, modelos y la superficie de tools del Anillo 0.
Sin efectos secundarios: importar este modulo no toca red ni ESCRIBE disco (la única
lectura es el ``escapement.toml`` opcional del usuario, ver :func:`_user_config`).
"""

from __future__ import annotations

import contextvars
import os
import re
from pathlib import Path
from typing import NamedTuple

import tomllib

# --- Identidad (nombre de marca, configurable) ---
AGENT_NAME: str = str(os.environ.get("AGENT_NAME", "Escapement").strip())
AGENT_SLUG: str = re.sub(r"[^a-z0-9]+", "-", AGENT_NAME.lower()).strip("-") or "agent"

# --- Modelos (IDs actuales, verificados) ---
MODEL_MAIN: str = "claude-sonnet-5"  # desarrollo estándar, razonamiento medio, auditorías ligeras
MODEL_HEAVY: str = "claude-opus-5"  # razonamiento duro, operar, auditar
MODEL_CHEAP: str = "claude-haiku-4-5-20251001"  # triage barato (futuro loop de voz)
# El planner asigna TODO el plan (tipos/personas -> modelos): su decisión gobierna el resto, así que
# corre SIEMPRE en un modelo online de esfuerzo medio (Sonnet), sólido — nunca local, ajeno al tiering
# y al ruteo local. Que un modelo local o barato reparta el trabajo saldría más caro que el ahorro.
# Único override respetado: EXECUTOR_MODEL (martillo manual de debugging end-to-end, no flag de ruteo).
# El ruteo local/online se aplica DESPUÉS, paso por paso, sobre este plan.
MODEL_PLANNER: str = MODEL_MAIN

# --- Executor del orquestador: el motor que aplica los refactors y juzga (dispatch/judge) ---
# claude (default) | antigravity (agy) | cursor. Permite que quien no use Claude aproveche el
# orquestador; el resto del pipeline es agnóstico. Los flags viven en agent/executors.py.
EXECUTOR: str = (os.environ.get("AGENT_EXECUTOR") or "claude").lower()

# Modelo del executor headless (SOLO claude -p). Vacío = el default de Claude Code. Setéalo
# (AGENT_EXECUTOR_MODEL, p.ej. "claude-opus-5") para forzar un modelo concreto en TODO dispatch
# del planner/runner/orquestador —probar un modelo nuevo end-to-end—. No afecta a agy/cursor.
# Lineup de Claude disponible hoy:
#   claude-opus-5                 razonamiento duro  (MODEL_HEAVY)
#   claude-sonnet-5               desarrollo estándar (MODEL_MAIN, y MODEL_PLANNER)
#   claude-haiku-4-5-20251001     triage barato      (MODEL_CHEAP)
# Fable (claude-fable-5*) queda DESACTIVADO por ahora: si AGENT_EXECUTOR_MODEL pide uno, se
# normaliza a MODEL_HEAVY en vez de despacharse. El silencio es a propósito — importar config no
# debe imprimir ni fallar. Para reactivarlo, borra _sin_fable y su llamada: no hay otro punto que
# filtre modelos.
_FABLE_OFF: tuple[str, ...] = ("claude-fable",)


def _sin_fable(modelo: str) -> str:
    """Devuelve ``modelo``, o ``MODEL_HEAVY`` si es un Fable (desactivado por ahora).

    Args:
        modelo: ID de modelo pedido, ya sin espacios de borde. La comparación es
            case-insensitive. Vacío significa "el default del CLI" y se devuelve tal
            cual: no se fuerza ningún modelo cuando no se pidió uno.
    """
    if modelo and modelo.lower().startswith(_FABLE_OFF):
        return MODEL_HEAVY
    return modelo


EXECUTOR_MODEL: str = _sin_fable((os.environ.get("AGENT_EXECUTOR_MODEL") or "").strip())

# Nº de jueces adversariales que votan cada diff antes del PR (Fase 0: verificación robusta).
# 1 (default) = un solo juez, comportamiento previo y sin cuota extra. Impar >1 (p.ej. 3) activa
# el voto por mayoría (self-consistency): reduce el falso-SEGURO de un veredicto aislado a costa
# de N× la cuota del juez. Empate o mayoría RIESGOSO -> bloquea (conservador).
try:
    JUDGE_VOTERS: int = max(1, int(os.environ.get("AGENT_JUDGE_VOTERS") or 1))
except ValueError:
    JUDGE_VOTERS = 1

# Techo de tiempo de UN dispatch al executor, en segundos. Era un literal enterrado en
# `executors.run_agent` y se quedaba corto: un paso pesado (portar una fase entera y escribir su
# suite) pasa de la media hora y el corte llega con el trabajo a medio commitear, con lo que el
# runner lo marca fallido aunque el entregable estuviera bien encaminado. Subir el reloj es
# preferible a partir el paso: el corte no mide la capacidad del modelo, mide el reloj. Piso de
# 60 s para que un valor absurdo en el entorno no deje el dispatch sin margen.
try:
    EXECUTOR_TIMEOUT: float = max(60.0, float(os.environ.get("AGENT_EXECUTOR_TIMEOUT") or 5400))
except ValueError:
    EXECUTOR_TIMEOUT = 5400.0

# --- Router híbrido (F1) + backend local del orquestador ---
# Los valores de LOCAL_LLM_* y el enrutado local/online (route_step) se definen más abajo, en la
# sección "[local]", porque se leen de env/escapement.toml y necesitan _CFG ya cargado.

# Raíz del repo de la app (contiene src/, models/); base del auto-conocimiento del agente.
REPO_ROOT: Path = Path(__file__).resolve().parents[2]


def _user_config() -> dict:
    """Carga el archivo de config del usuario (``escapement.toml``, opcional). ``{}`` si no hay.

    Desacopla el paquete de las rutas de UNA máquina (M1/H10): repos y rutas viven en un
    archivo local (gitignorado; plantilla en ``escapement.toml.example``), no en el código.
    Precedencia de cada valor: env var > archivo > default hardcodeado — sin archivo, el
    comportamiento es EXACTAMENTE el previo. Ubicación del archivo: ``AGENT_CONFIG_FILE``
    o ``<repo>/escapement.toml``. Best-effort: ilegible/corrupto = como si no existiera.
    """
    ruta = os.environ.get("AGENT_CONFIG_FILE") or str(REPO_ROOT / "escapement.toml")
    try:
        with open(ruta, "rb") as f:
            return tomllib.load(f)
    except (OSError, ValueError):  # TOMLDecodeError es subclase de ValueError
        return {}


_CFG: dict = _user_config()

# --- Sustrato de memoria: rutas REALES (no los junctions del vault) ---
HOME: Path = Path.home()
# Rutas configurables por env var o escapement.toml (portabilidad); fallback al layout de ~/.claude.
RECALL_SCRIPT: Path = Path(
    os.environ.get("AGENT_RECALL_SCRIPT")
    or _CFG.get("recall_script")
    or HOME / ".claude" / "tools" / "recall_memory.py"
)
PROJECTS_DIR: Path = Path(
    os.environ.get("AGENT_PROJECTS_DIR")
    or _CFG.get("projects_dir")
    or HOME / ".claude" / "projects"
)

# Modelos locales (voz, etc.) dentro del repo (models/ está en .gitignore).
MODELS_DIR: Path = REPO_ROOT / "models"

def _slug_de_ruta(ruta: str | Path) -> str:
    """Slug de vault: ruta absoluta con los no-alfanuméricos vueltos ``-``.

    Es la convención de Claude Code, que deriva la carpeta del cwd de la sesión.
    Fuente única: la usan :data:`AGENT_HOME` y :func:`vault_slug`, para que no
    puedan divergir y mandar las notas de un mismo repo a dos carpetas.
    """
    return re.sub(r"[^a-zA-Z0-9]", "-", os.path.abspath(str(ruta)))


# Memoria del agente: su propio vault, el MISMO que abre Claude Code al sesionar en el repo.
AGENT_HOME: Path = PROJECTS_DIR / _slug_de_ruta(REPO_ROOT)
AGENT_MEMORY: Path = (
    AGENT_HOME / "memory"
)  # notas markdown (conocimiento) — vault + junction Obsidian

# Datos operacionales (ledger, cola, conversaciones, sesión de voz): DENTRO del repo para tenerlos
# junto al código, pero gitignorados — pueden llevar rutas o diálogo personal y el repo tiene
# remoto en GitHub. Override con AGENT_DATA_DIR o escapement.toml. Las notas markdown NO viven aquí
# (siguen en el vault).
DATA_DIR: Path = Path(
    os.environ.get("AGENT_DATA_DIR") or _CFG.get("data_dir") or REPO_ROOT / "data"
)
LEDGER: Path = DATA_DIR / "ledger.jsonl"  # memoria operacional/episódica del orquestador
CONVERSATIONS: Path = DATA_DIR / "conversations.jsonl"  # turnos usuario<->agente (chat/voz)
EVENTS: Path = DATA_DIR / "events.jsonl"  # journal del bus de eventos (agent.bus)
PLAN: Path = DATA_DIR / "plan.json"  # roadmap del objetivo activo (planner, Fase A)


# Repositorios que gestiona el usuario (nombre -> ruta). Para tareas cross-repo (compilar memoria,
# auditar) Escapement sabe qué explorar sin que se los enumeres. El único default es el propio repo:
# los tuyos van en la tabla [repos] de escapement.toml (gitignorado), que añade y sobreescribe sobre
# este default. "escapement" siempre está presente.
#
# El NOMBRE importa: `personas.select` enruta el paso al coder de dominio comparando ese nombre
# contra los slots que declara cada persona ("scraper" -> persona `scraping`, "backend" ->
# `backend-app`). Nombra tu repo con el slot que le toque y el auto-ruteo sale solo; cualquier
# otro nombre cae al `ingeniero` genérico, que también es una respuesta válida.
_DEFAULT_REPOS: dict[str, str] = {
    "escapement": str(REPO_ROOT),
}
REPOS: dict[str, str] = {
    **_DEFAULT_REPOS,
    **{str(k): str(v) for k, v in dict(_CFG.get("repos") or {}).items()},
}

# Ajustes locales de las personas (ver agent.personas). El registro del código es el default
# público —prompts de dominio genéricos y slots de repo—; el binding repo->persona y el detalle
# del dominio son propios de cada instalación y se declaran en la tabla [personas] de
# escapement.toml (gitignorada), sin tocar el código. Ver personas._aplicar_overrides.
PERSONA_OVERRIDES: dict[str, dict[str, object]] = {
    str(k): dict(v) for k, v in dict(_CFG.get("personas") or {}).items() if isinstance(v, dict)
}


# --- Ramas protegidas: sobre cuáles el gate PROHIBE commit/push (ver agent.security.guard) ---
# Los defaults son las ramas de integración universales; la config solo AÑADE, nunca quita: una
# instalación puede proteger su rama de integración propia ("integracion", "develop", ...) pero
# NADIE puede desproteger main/master desde un archivo o una env var. Un gate que se apaga con
# config no es un gate. Declara las tuyas en [git] ramas_protegidas de escapement.toml, o en
# AGENT_PROTECTED_BRANCHES (nombres separados por coma), que se suma a las del archivo.
_DEFAULT_PROTECTED: tuple[str, ...] = ("main", "master")


def _ramas_extra() -> tuple[str, ...]:
    """Ramas protegidas ADICIONALES declaradas por el usuario, en orden y sin repetir."""
    crudo: list[str] = []
    valor = dict(_CFG.get("git") or {}).get("ramas_protegidas")
    if isinstance(valor, (list, tuple)):
        crudo += [str(x) for x in valor]
    crudo += (os.environ.get("AGENT_PROTECTED_BRANCHES") or "").split(",")
    vistas: list[str] = []
    for nombre in (x.strip() for x in crudo):
        if nombre and nombre not in _DEFAULT_PROTECTED and nombre not in vistas:
            vistas.append(nombre)
    return tuple(vistas)


PROTECTED_BRANCHES: tuple[str, ...] = _DEFAULT_PROTECTED + _ramas_extra()

# Candidatos a rama BASE de una entrega, en orden de preferencia (ver runner.default_branch). Las
# protegidas son por definición las de integración, así que se reusan; "develop" cierra la lista
# como convención git-flow sin estar protegida.
BASE_BRANCH_CANDIDATES: tuple[str, ...] = PROTECTED_BRANCHES + (
    () if "develop" in PROTECTED_BRANCHES else ("develop",)
)


# --- Identidad del usuario: quién es el "tú" de los system prompts (prompts/system.md, router) ---
# El código público no lleva el nombre de nadie: sin config, los prompts dicen "tu usuario" y
# siguen siendo gramaticales. Declara el tuyo en la tabla [usuario] de escapement.toml (gitignorada)
# o por env (AGENT_USER_NAME / AGENT_USER_PROFILE).
def _usuario(clave: str, env: str) -> str:
    """Un campo de identidad del usuario: env var > tabla ``[usuario]`` del toml > ``''``."""
    valor = os.environ.get(env) or dict(_CFG.get("usuario") or {}).get(clave) or ""
    return str(valor).strip()


# Cómo llamar al usuario en un prompt. Sin configurar, la etiqueta genérica: el prompt dice
# "la pregunta a tu usuario" en vez de nombrar a nadie.
USER_NAME: str = _usuario("nombre", "AGENT_USER_NAME") or "tu usuario"
# Aposición opcional que enmarca su oficio, YA FORMATEADA para pegarse tras el nombre
# (" (operations / scraping)"). Vacía si no se declara, para no dejar un paréntesis huérfano.
_PERFIL: str = _usuario("perfil", "AGENT_USER_PROFILE")
USER_PROFILE: str = f" ({_PERFIL})" if _PERFIL else ""


# --- Budget por plan (R3): tope opcional de tokens ESTIMADOS por corrida del runner ---
# None (default) = sin tope, comportamiento previo. Un entero positivo (env
# AGENT_BUDGET_TOKENS_PER_PLAN, o [budget] tokens_por_plan en escapement.toml) hace que el runner deje
# de despachar pasos y los marque 'bloqueado' cuando el gasto ESTIMADO del plan lo supera. Es un
# proxy (~4 chars/token), no una factura real (ver agent/costs.py). Basura/<=0 = None (no-op).
def _budget_tokens() -> int | None:
    raw = os.environ.get("AGENT_BUDGET_TOKENS_PER_PLAN")
    if raw is None:
        raw = dict(_CFG.get("budget") or {}).get("tokens_por_plan")
    try:
        val = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
    return val if val and val > 0 else None


BUDGET_TOKENS_PER_PLAN: int | None = _budget_tokens()


# --- Checkpoint de aprobación del plan (M1): leer el roadmap antes de gastar presupuesto ---
# True (default): `escapement ejecutar` muestra el roadmap completo y pide confirmación ANTES de la
# PRIMERA corrida de un plan. Una vez despachado un paso ya no vuelve a preguntar (reanudar tras un
# checkpoint es continuar algo aprobado, no una decisión nueva). Solo aplica en terminal
# INTERACTIVA: sin TTY (cron, pipeline autónomo) ejecuta sin preguntar, igual que antes. False =
# nunca preguntar. Override por corrida: `escapement ejecutar --si`.
def _plan_flag(env: str, cfg_key: str, default: bool) -> bool:
    raw = os.environ.get(env)
    if raw is None:
        raw = dict(_CFG.get("plan") or {}).get(cfg_key)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


PLAN_APPROVAL: bool = _plan_flag("AGENT_PLAN_APPROVAL", "aprobacion", True)

# --- Evaluación global al cerrar (E2, deuda #7): el conteo de pasos no es el done real ---
# True (default): cuando el roadmap se agota con todos los pasos 'hecho' y el plan tiene
# `criterio_global`, el runner despacha una evaluación READ-ONLY que contrasta el resultado contra
# ese criterio. Veredicto 'done' o 'incompleto' + gaps concretos (quedan persistidos en el plan,
# insumo de la replanificación). Best-effort: sin salida usable, el cierre queda como antes.
# False (AGENT_PLAN_EVAL=0 / [plan] evaluacion): comportamiento previo exacto (completo = conteo).
PLAN_EVAL: bool = _plan_flag("AGENT_PLAN_EVAL", "evaluacion", True)

# --- Replanificación automática (E2, deuda #7): los gaps del veredicto se vuelven pasos ---
# Tope de CICLOS de replanificación por plan. Con veredicto 'incompleto', el runner convierte los
# gaps en pasos nuevos (vía el mecanismo de `reflexionar`) y sigue ejecutando — hasta este tope.
# El contador vive en el plan (`replan_ciclos`, persistido): reanudar una corrida NO lo resetea.
# Al alcanzar el tope la corrida termina 'incompleta' con los gaps guardados = checkpoint humano.
# 0 = replanificación apagada (solo evalúa, comportamiento de la pieza E2-1). No es `_pos_int`
# porque ahí 0 caería al default y aquí 0 es un valor significativo; basura/negativo = default.
_REPLAN_MAX_DEFAULT = 2


def _replan_max() -> int:
    raw = os.environ.get("AGENT_PLAN_REPLAN_MAX")
    if raw is None:
        raw = dict(_CFG.get("plan") or {}).get("replan_max")
    try:
        val = int(raw) if raw is not None else _REPLAN_MAX_DEFAULT
    except (TypeError, ValueError):
        return _REPLAN_MAX_DEFAULT
    return val if val >= 0 else _REPLAN_MAX_DEFAULT


PLAN_REPLAN_MAX: int = _replan_max()


# --- Swarm (R4): fan-out de subagentes en paralelo para un paso 'swarm' (investigación amplia) ---
# El handler descompone la tarea en subtareas INDEPENDIENTES y las despacha CONCURRENTES (read-only,
# sin choque de escrituras sobre el worktree del plan). Dos topes configurables (env o [swarm] en
# escapement.toml), ambos con defaults conservadores; basura/<=0 cae al default (nunca 0 hilos ni 0
# subtareas). Bajar max_workers a 1 serializa el fan-out sin desactivarlo.
def _pos_int(env: str, cfg_key: str, default: int) -> int:
    raw = os.environ.get(env)
    if raw is None:
        raw = dict(_CFG.get("swarm") or {}).get(cfg_key)
    try:
        val = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


SWARM_MAX_WORKERS: int = _pos_int("AGENT_SWARM_MAX_WORKERS", "max_workers", 4)
SWARM_MAX_TASKS: int = _pos_int("AGENT_SWARM_MAX_TASKS", "max_subtareas", 6)


# --- Enrutado de modelo POR TAREA (ahorro de tokens) ---
# Cada dispatch usa el modelo del TAMAÑO de su trabajo en vez de uno solo para todo: HEAVY para
# razonamiento duro (planner, auditorías), MAIN para código estándar, CHEAP para lo mecánico
# (formatear notas, triage). OFF por default (no-op, comportamiento previo intacto); préndelo con
# AGENT_MODEL_TIERING=1 o [modelos] tiering=true en escapement.toml. EXECUTOR_MODEL, si está seteado,
# GANA sobre el tiering (override manual duro para probar un modelo en TODO). Solo afecta a claude.
def _flag(env: str, cfg_key: str, default: bool) -> bool:
    raw = os.environ.get(env)
    if raw is None:
        raw = dict(_CFG.get("modelos") or {}).get(cfg_key)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


MODEL_TIERING: bool = _flag("AGENT_MODEL_TIERING", "tiering", False)

# Personas de razonamiento duro: sus pasos suben a MODEL_HEAVY aunque el tipo sea barato (una
# auditoría de seguridad o una decisión de arquitectura merecen el modelo grande).
HEAVY_PERSONAS: frozenset[str] = frozenset(
    {"seguridad", "arquitecto", "optimizador", "depurador", "migraciones", "datos"}
)

# tipo de paso -> tier por defecto cuando el tiering está ON (la persona dura puede subirlo a HEAVY).
_TIER_POR_TIPO: dict[str, str] = {
    "planner": MODEL_PLANNER,  # coherencia si algo llama model_for('planner'); planner.plan() usa MODEL_PLANNER directo
    "editar": MODEL_MAIN,
    "crear": MODEL_MAIN,
    "investigar": MODEL_MAIN,
    "ejecutar": MODEL_MAIN,
    "verificar": MODEL_MAIN,
    "reflexionar": MODEL_MAIN,
    "swarm": MODEL_MAIN,  # rama de investigación real
    "memoria": MODEL_CHEAP,  # formatear/organizar notas
    "triage": MODEL_CHEAP,  # descomposición del swarm y clasificaciones baratas
    "evaluar": MODEL_MAIN,  # veredicto del criterio global al cerrar el plan (E2, deuda #7)
}


def model_for(kind: str, persona: str = "") -> str:
    """Modelo (``--model``) para un dispatch según su tipo de trabajo; '' = default del CLI.

    Args:
        kind: tipo de paso del runner (editar/crear/investigar/ejecutar/verificar/reflexionar/
            swarm/memoria) o un pseudo-tipo del pipeline ('planner', 'triage'). Desconocido -> MAIN.
        persona: persona asignada al paso; si es de :data:`HEAVY_PERSONAS`, sube el tier a HEAVY.

    Devuelve '' cuando el tiering está OFF y no hay ``EXECUTOR_MODEL`` (no-op, comportamiento
    previo: el CLI usa su modelo por defecto). ``EXECUTOR_MODEL`` explícito gana sobre el tiering.
    """
    if EXECUTOR_MODEL:  # override manual duro: un modelo fijo para TODO dispatch
        return EXECUTOR_MODEL
    if not MODEL_TIERING:  # OFF: no-op, el CLI elige (comportamiento previo)
        return ""
    if persona and persona.strip().lower() in HEAVY_PERSONAS:
        return MODEL_HEAVY
    return _TIER_POR_TIPO.get(kind, MODEL_MAIN)


# --- [local]: LLM local (Ollama) — config por env/escapement.toml + enrutado estratégico ---
# Precedencia de cada valor: env var > [local] en escapement.toml > default hardcodeado. El ruteo
# estratégico local/online del orquestador está SIEMPRE activo (no hay flag de activación): route_step
# reparte cada paso por dificultad. Para apagar TODO lo local (voz incluida) -> LOCAL_LLM_ENABLED=False.
def _local_raw(cfg_key: str) -> object | None:
    return dict(_CFG.get("local") or {}).get(cfg_key)


def _local_str(env: str, cfg_key: str, default: str) -> str:
    raw = os.environ.get(env)
    if raw is None:
        raw = _local_raw(cfg_key)
    return str(raw).strip() if raw is not None else default


def _local_flag(env: str, cfg_key: str, default: bool) -> bool:
    raw = os.environ.get(env)
    if raw is None:
        raw = _local_raw(cfg_key)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _local_int(env: str, cfg_key: str, default: int) -> int:
    raw = os.environ.get(env)
    if raw is None:
        raw = _local_raw(cfg_key)
    try:
        val = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


LOCAL_LLM_ENABLED: bool = _local_flag("AGENT_LOCAL_LLM_ENABLED", "enabled", True)
LOCAL_LLM_BASE_URL: str = _local_str(
    "AGENT_LOCAL_LLM_BASE_URL", "base_url", "http://localhost:11434/v1"
)  # Ollama, API OpenAI-compat
LOCAL_LLM_MODEL: str = _local_str(
    "AGENT_LOCAL_LLM_MODEL", "model", "qwen2.5:3b"
)  # LIGERO: instruct rápido para voz y VRAM ocupada
LOCAL_LLM_MODEL_MEDIUM: str = _local_str(
    "AGENT_LOCAL_LLM_MODEL_MEDIUM", "model_medium", "qwen2.5-coder:7b"
)  # MEDIO: solo con VRAM holgada y voz inactiva
LOCAL_LLM_KEEP_ALIVE: str = _local_str(
    "AGENT_LOCAL_LLM_KEEP_ALIVE", "keep_alive", "5m"
)  # Ollama descarga el modelo tras inactividad (libera VRAM para STT)
LOCAL_LLM_KEEP_ALIVE_BUSY: str = _local_str(
    "AGENT_LOCAL_LLM_KEEP_ALIVE_BUSY", "keep_alive_busy", "0"
)  # descarga inmediata cuando el local compite con la voz por la VRAM
LOCAL_LLM_TIMEOUT_S: int = _local_int(
    "AGENT_LOCAL_LLM_TIMEOUT", "timeout_s", 60
)  # tope duro (s) de una request local; al superarlo -> None y fallback a Claude

# Auto-selección del modelo local según hardware (VRAM libre). OFF => siempre el LIGERO.
LOCAL_AUTO_SELECT: bool = _local_flag("AGENT_LOCAL_AUTO_SELECT", "auto_select", True)
LOCAL_VRAM_MEDIUM_MB: int = _local_int(
    "AGENT_LOCAL_VRAM_MEDIUM_MB", "vram_medium_mb", 5600
)  # VRAM libre mínima para subir al modelo MEDIO (7b Q4 ~4.7GB + margen KV)

# El ruteo estratégico del orquestador (Fase 1: razonamiento read-only reflexionar/triage en local)
# está SIEMPRE activo: no hay flag de activación. La calidad se protege con el fallback a Claude cuando
# la salida local no valida, y con el veto de voz de abajo. Para apagarlo del todo: LOCAL_LLM_ENABLED=False.
LOCAL_ORCH_TOOLS: bool = _local_flag(
    "AGENT_LOCAL_ORCH_TOOLS", "orchestrator_tools", False
)  # Fase 2 (aún sin implementar): investigar/verificar read-only con tool-loop local. Default OFF hasta que exista
# Veto de GPU: si la sesión es por VOZ (Whisper+Kokoro ya ocupan la VRAM), el orquestador va 100%
# online para no colapsar los 8GB. Determinista (bandera de proceso), no heurístico.
LOCAL_ORCH_DISABLE_ON_VOICE: bool = _local_flag(
    "AGENT_LOCAL_ORCH_DISABLE_ON_VOICE", "orchestrator_disable_on_voice", True
)

# Bandera de proceso de "sesión por voz": la setea el entrypoint de voz (daemon). route_step
# la consulta para vetar el local en el orquestador. ContextVar => thread-safe y sin estado global sucio.
_VOICE_SESSION: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "escapement_voice_session", default=False
)


def set_voice_session(active: bool) -> None:
    """Marca (o desmarca) que la interacción actual es por voz. La llaman los entrypoints de voz."""
    _VOICE_SESSION.set(bool(active))


def voice_session_active() -> bool:
    """True si el proceso corre una sesión de voz (Whisper+Kokoro ocupan la VRAM)."""
    return _VOICE_SESSION.get()


class Route(NamedTuple):
    """Decisión de despacho de un paso del orquestador.

    Args:
        backend: ``"local"`` (LLM local in-process) o ``"cli"`` (executor online: claude/agy/cursor).
        model: ``--model`` para el backend ``"cli"``; ``""`` = default del CLI. Para ``"local"`` va ``""``
            (el modelo concreto lo elige :func:`agent.local_models.select_local_model` según hardware).
    """

    backend: str
    model: str


# Tipos de paso elegibles para el LLM local (razonamiento/lectura, nunca edición).
_LOCAL_ELIGIBLE: frozenset[str] = frozenset(
    {"memoria", "reflexionar", "triage", "investigar", "verificar", "swarm", "evaluar"}
)
# Clase B: exploran el repo (necesitan Read/Glob/Grep) => solo local con tool-loop (Fase 2).
_LOCAL_CLASE_B: frozenset[str] = frozenset({"investigar", "verificar", "swarm", "evaluar"})
# Señales de que el paso corre comandos/shell (build/test/git): jamás apto para el local read-only.
_NEEDS_SHELL: re.Pattern[str] = re.compile(
    r"\b(ejecuta|corre|instal|compil|build|pytest|test|npm|uv |pip |docker|git |lint|deploy|migrat)\w*",
    re.IGNORECASE,
)


def route_step(kind: str, persona: str = "", accion: str = "", done: str = "") -> Route:
    """Enruta un paso del orquestador a LOCAL u online (CLI) según su dificultad y el hardware.

    El ruteo está SIEMPRE activo (sin flag de activación). Estrategia (de la regla más fuerte a la más
    débil):
        1. Sesión por VOZ y ``LOCAL_ORCH_DISABLE_ON_VOICE`` -> CLI (veto de GPU: no sumar razonamiento
           local sobre la VRAM que ya usan Whisper+Kokoro).
        2. Persona de :data:`HEAVY_PERSONAS` -> CLI (razonamiento duro: siempre Claude).
        3. ``kind`` no elegible (editar/crear/ejecutar/planner) -> CLI (edición: nunca local).
        4. Señal de razonamiento sustancial o turno largo en el texto -> CLI (dificultad alta).
        5. Clase B (investigar/verificar/swarm) sin ``LOCAL_ORCH_TOOLS``, o con señales de shell -> CLI.
        6. Local disponible -> LOCAL; si no -> CLI (fallback duro).

    Args:
        kind: tipo de paso del runner (o pseudo-tipo 'triage'/'planner').
        persona: persona asignada; si es HEAVY, fuerza CLI.
        accion, done: texto del paso; alimenta la heurística de dificultad y el guard de shell.

    Returns:
        :class:`Route`. El ``model`` del backend CLI sale de :func:`model_for` (respeta el tiering).
    """
    cli = Route("cli", model_for(kind, persona))
    if LOCAL_ORCH_DISABLE_ON_VOICE and voice_session_active():
        return cli
    if persona and persona.strip().lower() in HEAVY_PERSONAS:
        return cli
    if kind not in _LOCAL_ELIGIBLE:
        return cli
    texto = f"{accion} {done}".strip()
    # Heurística de dificultad: reutiliza las señales de razonamiento del router conversacional.
    from agent import router  # import diferido: evita ciclo config<->router

    if router._NEEDS_REASONING.search(texto) or len(texto.split()) > router._LONG_TURN_WORDS:
        return cli
    if kind in _LOCAL_CLASE_B and (not LOCAL_ORCH_TOOLS or _NEEDS_SHELL.search(texto)):
        return cli
    from agent import local_models  # import diferido: evita ciclo config<->local_models

    return Route("local", "") if local_models.available() else cli


def vault_slug(repo_path: str) -> str:
    """Carpeta canónica del vault para un repo (misma convención que Claude Code y ``_memoria_hecha``).

    El vault de cada repo vive en ``PROJECTS_DIR/<slug>/memory``; el slug es la ruta absoluta con
    los no-alfanuméricos vueltos ``-`` (p.ej. ``G:\\mi_proyecto`` -> ``G--mi-proyecto``).
    SIN excepciones, el propio repo del agente incluido: Claude Code deriva el slug del cwd de la
    sesión, así que un caso especial mandaría las notas del agente a una carpeta que el índice de
    sesión nunca carga. Fuente única para que el runner escriba SIEMPRE donde alguien las lee.
    """
    return _slug_de_ruta(repo_path)


# --- Anillo 0: auto-aprobadas (read-only / consulta, sin efectos locales) ---
# Van en allowed_tools -> no piden confirmación. Edita esta lista para ajustar cuán
# laxo es el gate; las acciones CON efectos (Bash, Write, operar) siempre confirman
# aparte, y el guard hook (SQL/ramas/.env) bloquea sin importar esta lista.
# Las tools in-process del server "memory" se exponen como mcp__memory__<tool>.
RING0_TOOLS: tuple[str, ...] = (
    "Read",
    "Glob",
    "Grep",
    "mcp__memory__recall_memory",  # memoria vectorial read-only (sin efectos locales)
    "mcp__memory__list_memory",  # inventario de notas del vault (read-only)
    "mcp__memory__read_memory",  # contenido de una nota por slug (read-only)
    "mcp__memory__list_repos",  # repos que gestiona el usuario (read-only)
    "mcp__semantic__recall_semantic",  # consulta semántica read-only (vector DB, sin efectos locales)
    "mcp__orq__estado",  # orquestador read-only: estado de cola/ledger/PRs
    "mcp__orq__deuda",  # orquestador read-only: lista la deuda de un repo
    "mcp__orq__plan",  # orquestador read-only: roadmap del plan y su avance
    "WebSearch",  # consulta web read-only (egress a internet, sin efectos locales)
    "WebFetch",
    "WebScrape",  # fetch + parse HTML (egress a internet, sin efectos locales)
)
# NOTA: las acciones CON efectos (Bash, Write/Edit, y el orquestador optimizar/vigilar/trabajar/
# revisar_prs/objetivo/ejecutar/paso) NO van aquí: pasan por el gate con "aprobar-una-vez por
# categoría" (una confirmación cubre las siguientes de su tipo en la sesión). Auto-aprobar Bash
# daría shell arbitrario sin OK.


# --- Voz (F2) ---
# STT en GPU (la RTX 3050 sirve, float16); auto-detect para es/en técnico. Default:
# large-v3-turbo — multilingüe, ~1.6 GB de VRAM en float16 (large-v3 usa ~3 GB), decodificación
# ~4x más rápida y pérdida de calidad mínima. OJO: los modelos distil-* son SOLO inglés; no
# sirven para el code-switching es/en de este flujo. Precedencia de cada valor STT:
# env var > [voice] en escapement.toml > default.
def _voice_str(env: str, cfg_key: str, default: str) -> str:
    """Resuelve un valor string de voz: env var > ``[voice]`` en escapement.toml > default.

    Args:
        env: nombre de la variable de entorno; si existe gana siempre (aunque sea ``""``).
        cfg_key: clave dentro de la tabla ``[voice]`` de escapement.toml.
        default: valor cuando no hay env ni entrada en el archivo.
    """
    raw = os.environ.get(env)
    if raw is None:
        raw = dict(_CFG.get("voice") or {}).get(cfg_key)
    return str(raw).strip() if raw is not None else default


def _voice_flag(env: str, cfg_key: str, default: bool) -> bool:
    """Resuelve un booleano de voz: env var > ``[voice]`` en escapement.toml > default.

    Args:
        env: nombre de la variable de entorno; si existe gana ("1"/"true"/"yes"/"on" = True,
            cualquier otro valor = False).
        cfg_key: clave dentro de la tabla ``[voice]`` de escapement.toml.
        default: valor cuando no hay env ni entrada en el archivo.
    """
    raw = os.environ.get(env)
    if raw is None:
        raw = dict(_CFG.get("voice") or {}).get(cfg_key)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _voice_float(env: str, cfg_key: str, default: float) -> float:
    """Resuelve un float de voz: env var > ``[voice]`` en escapement.toml > default.

    Args:
        env: nombre de la variable de entorno; si existe tiene prioridad.
        cfg_key: clave dentro de la tabla ``[voice]`` de escapement.toml.
        default: valor cuando no hay env/archivo o cuando el valor no parsea como float.
    """
    raw = os.environ.get(env)
    if raw is None:
        raw = dict(_CFG.get("voice") or {}).get(cfg_key)
    try:
        return float(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default


VOICE_STT_MODEL: str = _voice_str("AGENT_VOICE_STT_MODEL", "stt_model", "large-v3-turbo")
VOICE_STT_DEVICE: str = _voice_str(
    "AGENT_VOICE_STT_DEVICE", "stt_device", "cuda"
)  # fallback automático a cpu/int8 si la GPU no está disponible
VOICE_STT_COMPUTE: str = _voice_str(
    "AGENT_VOICE_STT_COMPUTE", "stt_compute", "int8_float16"
)  # ~1.2 GB de VRAM con turbo, contra ~2.2 GB de "float16", y en la práctica más RÁPIDO: en una
# GPU de 8 GB compartida con el escritorio, float16 deja tan poco margen que CUDA desborda a RAM
# del sistema y una transcripción de 6 s salta de ~1 s a 17-60 s. Medido: mediana 1.04 s (peor
# 1.17) contra 1.40 s (peor 63) con la misma frase. En una GPU holgada, "float16" es marginalmente
# más preciso y ahí sí conviene ponerlo explícito.
VOICE_LANG: str = "es"  # idioma FIJO del STT: se lo pasa a faster-whisper, que así se salta la
# detección de idioma. Ponerlo en None reactiva la auto-detección (es/en, para code-switching) a
# costa de una pasada extra sobre los primeros 30 s de audio.
VOICE_STT_PROMPT: str | int = (
    "Terminología técnica común: scraping, taskkill, pull request, branch, main, "
    "deploy, commit, config, proxy, endpoint, Chrome, PowerShell, script."
)
VOICE_HOTKEY: str = "f2"  # hotkey de voz, remapeable; su comportamiento lo fija VOICE_PTT_MODE
VOICE_TTS_ENGINE: str = "kokoro"  # "kokoro" (natural, kokoro-onnx) | "sapi" (fallback nativo)
VOICE_TTS_VOICE: str = "ef_dora"  # kokoro español: ef_dora (fem), em_alex (masc)
VOICE_KOKORO_LANG: str = "es"
VOICE_KOKORO_ONNX: Path = MODELS_DIR / "kokoro-v1.0.fp16.onnx"
VOICE_KOKORO_VOICES: Path = MODELS_DIR / "voices-v1.0.bin"
VOICE_TTS_RATE: int = 0  # velocidad SAPI (solo fallback), -10..10
# Silencio (s) que se añade al final del audio Kokoro: sd.wait() retorna antes de vaciar el
# buffer de salida del hardware y corta la última sílaba; esta cola garantiza que suene completa.
VOICE_TTS_TAIL_SILENCE: float = 0.3
# Barge-in: presionar la hotkey (F2) mientras Escapement habla corta la reproducción en el acto
# (el daemon pasa el chequeo de la hotkey como condición de corte a tts.speak). False =
# comportamiento previo: el TTS no se puede interrumpir.
VOICE_BARGE_IN: bool = _voice_flag("AGENT_VOICE_BARGE_IN", "barge_in", True)
# Habla en streaming: parte la respuesta en frases y las va diciendo mientras el modelo sigue
# escribiendo (el SDK emite los deltas con include_partial_messages). Sin esto Escapement calla hasta
# tener la respuesta ENTERA: en una respuesta larga son varios segundos de silencio. False =
# comportamiento previo exacto (una sola reproducción al final, sin partial messages).
VOICE_STREAM_TTS: bool = _voice_flag("AGENT_VOICE_STREAM_TTS", "stream_tts", True)
# Sesión abierta: el primer F2 abre una conversación y el micrófono se REABRE solo tras cada
# respuesta, sin pedir otra pulsación; F2 con el micrófono ya abierto la cierra. La sesión también
# se cierra sola si nadie habla en ~45 s, así que olvidarse de cerrarla no deja el micrófono vivo.
# False = comportamiento previo exacto: una pulsación de F2 por turno.
VOICE_OPEN_SESSION: bool = _voice_flag("AGENT_VOICE_OPEN_SESSION", "open_session", True)
# Wake word (manos libres, opcional). "" (default) = apagada: solo push-to-talk F2,
# comportamiento previo exacto. Un valor la activa: nombre de un modelo preentrenado de
# openWakeWord ("hey_jarvis", "alexa", "hey_mycroft", ...) o ruta a un .onnx custom. Con wake
# word el daemon acepta AMBOS gatillos (F2 o la frase) y el micrófono queda abierto mientras
# espera (detección 100% local en CPU, modelo <1 MB). Si openwakeword no está instalado o el
# modelo no carga, el daemon degrada solo a F2.
VOICE_WAKE_WORD: str = _voice_str("AGENT_VOICE_WAKE_WORD", "wake_word", "")
# Score mínimo (0-1) para dar la frase por detectada; subirlo reduce falsos positivos.
VOICE_WAKE_THRESHOLD: float = _voice_float("AGENT_VOICE_WAKE_THRESHOLD", "wake_threshold", 0.5)
# Modo de la hotkey de voz. "toggle" (default): interruptor — un toque ABRE la grabación, que se
# cierra con otro toque, tras ~2 s de silencio (mismo endpointing RMS del manos libres) o al tope
# de seguridad; no hay que mantener nada presionado. "hold": el push-to-talk clásico — graba solo
# mientras la tecla siga presionada. Cualquier otro valor cae a "toggle" (un typo en escapement.toml
# no debe tumbar el arranque del daemon).
_PTT_MODE_RAW = _voice_str("AGENT_VOICE_PTT_MODE", "ptt_mode", "toggle").lower()
VOICE_PTT_MODE: str = _PTT_MODE_RAW if _PTT_MODE_RAW in ("hold", "toggle") else "toggle"


def voice_ptt_verbo() -> str:
    """Verbo de la instrucción de la hotkey según el modo: "mantén" (hold) o "presiona" (toggle)."""
    return "mantén" if VOICE_PTT_MODE == "hold" else "presiona"
