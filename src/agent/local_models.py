"""Backend LLM local in-process del orquestador (F1, Clase A) + auto-selección por hardware.

Extiende el uso del modelo local (Ollama/Qwen) más allá del router conversacional de :mod:`agent.router`:
aquí vive el *backend* que el runner invoca para pasos de RAZONAMIENTO/lectura (reflexionar, triage),
la **selección automática del modelo** según la VRAM libre y el estado de la voz, y la contabilidad de
tokens locales (coste 0 frente al budget de Claude).

Diseño:
- Reutiliza :func:`agent.router._available` (ping cacheado a Ollama) y :func:`agent.router._strip_think`
  (fuente única, evita divergencia con el router de voz).
- ``complete`` NUNCA lanza: red caída/timeout/JSON basura -> ``None`` y el caller cae a Claude.
- Sin dependencias nuevas: ``nvidia-smi`` viene con el driver; ``openai`` ya está en el grupo ``local``.
- Sin efectos de importación (no toca red ni GPU al importar); todo es perezoso.
"""

from __future__ import annotations

import json
import subprocess
import time

from agent import config, router

# --- Auto-selección por hardware: VRAM libre + estado de la voz ---------------------------------

# Cache de VRAM: nvidia-smi cuesta ~30-80ms; cachear evita shell-ear en cada paso del plan.
_VRAM_CACHE_TTL_S: float = 20.0
_vram_cache: tuple[float, int | None] | None = None  # (monotonic_ts, vram_mb | None)

# Cache de modelos instalados en Ollama (GET /api/tags): sirve para no elegir el MEDIO si no está
# descargado (cada paso 404earía y caería a Claude pagando un ida-y-vuelta muerto). Los modelos
# instalados cambian raro -> TTL holgado; ante cualquier fallo la lista es vacía (=> degradar a ligero).
_TAGS_CACHE_TTL_S: float = 300.0
_tags_cache: tuple[float, frozenset[str]] | None = None  # (monotonic_ts, nombres instalados)

# Re-prueba de disponibilidad de Ollama: router._available es lru_cache one-shot (bien para la voz,
# sesión corta), pero el orquestador corre planes de minutos/horas y el estado de Ollama puede
# cambiar a mitad (se cae, o arranca tarde). Cada _AVAIL_PROBE_TTL_S invalidamos ese ping cacheado
# para re-verificar; así un plan largo no congela la decisión 'disponible' tomada en el primer paso.
_AVAIL_PROBE_TTL_S: float = 45.0
_avail_probe_ts: float | None = (
    None  # monotonic de la última re-prueba forzada (None = re-probar ya)
)


def reset_cache() -> None:
    """Invalida los caches de VRAM, tags y disponibilidad. Para tests y tras un cambio de carga."""
    global _vram_cache, _tags_cache, _avail_probe_ts
    _vram_cache = None
    _tags_cache = None
    _avail_probe_ts = None


def free_vram_mb() -> int | None:
    """VRAM libre (MB) del GPU 0 vía ``nvidia-smi``. ``None`` si no hay GPU/driver o timeout.

    ``None`` se trata aguas arriba como "VRAM baja" (sesga al modelo ligero): sin lectura fiable,
    conservador. Cacheado ``_VRAM_CACHE_TTL_S`` para no lanzar el subproceso en cada paso.

    Returns:
        VRAM libre en MB, o ``None`` si no se pudo medir (sin GPU NVIDIA, driver ausente, timeout,
        salida inesperada).
    """
    global _vram_cache
    now = time.monotonic()
    if _vram_cache is not None and (now - _vram_cache[0]) < _VRAM_CACHE_TTL_S:
        return _vram_cache[1]
    vram: int | None = None
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
        if r.returncode == 0:
            first = (r.stdout or "").strip().splitlines()
            if first:
                vram = int(first[0].strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        vram = None
    _vram_cache = (now, vram)
    return vram


def voice_recently_active(window_s: float = 600.0) -> bool:
    """Best-effort: ``True`` si la voz cargó modelos hace poco (probablemente aún ocupan VRAM).

    Lee ``data/voice_session.json`` (lo escribe el daemon de voz con ``last_activity`` epoch). El
    ``window_s`` por defecto (600 s) coincide con ``voice.daemon.FREEZE_AFTER``: tras esa inactividad
    el daemon descarga los modelos y libera la VRAM. Sin archivo o ilegible -> ``False``.

    Args:
        window_s: ventana en segundos desde la última actividad de voz para considerarla "activa".
    """
    try:
        raw = (config.DATA_DIR / "voice_session.json").read_text(encoding="utf-8")
        last = float(json.loads(raw).get("last_activity", 0.0))
    except (OSError, ValueError, TypeError):
        return False
    return last > 0.0 and (time.time() - last) < window_s


def _pulled_models() -> frozenset[str]:
    """Nombres de los modelos instalados en Ollama (``GET /api/tags``), cacheado ``_TAGS_CACHE_TTL_S``.

    Reutiliza el endpoint y el patrón de ping de :func:`agent.router._available`. Ante CUALQUIER
    fallo (red caída, timeout, JSON inesperado) devuelve un ``frozenset`` vacío: el caller trata "no
    verificable" igual que "ausente" y degrada al modelo ligero. NUNCA lanza.
    """
    global _tags_cache
    now = time.monotonic()
    if _tags_cache is not None and (now - _tags_cache[0]) < _TAGS_CACHE_TTL_S:
        return _tags_cache[1]
    nombres: frozenset[str] = frozenset()
    try:
        import httpx

        url = config.LOCAL_LLM_BASE_URL.replace("/v1", "/api/tags")
        data = httpx.get(url, timeout=2.0).json()
        nombres = frozenset(
            m["name"] for m in data.get("models", []) if isinstance(m, dict) and m.get("name")
        )
    except Exception:  # noqa: BLE001 - best-effort; sin lista fiable -> degradar a ligero
        nombres = frozenset()
    _tags_cache = (now, nombres)
    return nombres


def _esta_instalado(modelo: str, instalados: frozenset[str]) -> bool:
    """``True`` si ``modelo`` figura entre los ``instalados`` (tolera el tag ``:latest`` implícito)."""
    return modelo in instalados or (":" not in modelo and f"{modelo}:latest" in instalados)


def select_local_model() -> str:
    """Elige el modelo local a usar AHORA según auto-selección, estado de la voz y VRAM libre.

    Devuelve el LIGERO (:data:`config.LOCAL_LLM_MODEL`) cuando: la auto-selección está OFF, la sesión
    es por voz o la voz estuvo activa hace poco, la VRAM libre es baja (``< LOCAL_VRAM_MEDIUM_MB``) o
    no se pudo medir, o el MEDIO no está descargado en Ollama (o no se pudo verificar). Solo con VRAM
    holgada, voz inactiva y el MEDIO confirmado como instalado sube al MEDIO
    (:data:`config.LOCAL_LLM_MODEL_MEDIUM`). Conservador por diseño: en duda, ligero.
    """
    if not config.LOCAL_AUTO_SELECT:
        return config.LOCAL_LLM_MODEL
    if config.voice_session_active() or voice_recently_active():
        return config.LOCAL_LLM_MODEL
    vram = free_vram_mb()
    if vram is None or vram < config.LOCAL_VRAM_MEDIUM_MB:
        return config.LOCAL_LLM_MODEL
    medio = config.LOCAL_LLM_MODEL_MEDIUM
    # Solo subimos al MEDIO con confirmación POSITIVA de que está pulled: si no está (o /api/tags no
    # respondió -> set vacío), quedarse en el ligero evita 404ear cada paso del plan y caer a Claude.
    if not _esta_instalado(medio, _pulled_models()):
        return config.LOCAL_LLM_MODEL
    return medio


def _keep_alive() -> str:
    """keep_alive para Ollama: descarga inmediata si la voz compite por la VRAM; si no, el normal."""
    if config.voice_session_active() or voice_recently_active():
        return config.LOCAL_LLM_KEEP_ALIVE_BUSY
    return config.LOCAL_LLM_KEEP_ALIVE


def _clear_router_available() -> None:
    """Limpia el ping cacheado (lru_cache) de :func:`agent.router._available`, si lo tiene.

    Guardado con ``getattr``: en tests ``_available`` suele mockearse con un lambda sin ``cache_clear``.
    """
    clear = getattr(router._available, "cache_clear", None)
    if clear is not None:
        clear()


def _es_error_de_conexion(exc: BaseException) -> bool:
    """``True`` si ``exc`` es un fallo de RED/timeout de Ollama (no un 404/JSON del modelo).

    Solo estos justifican re-probar disponibilidad: un modelo ausente (404) o una respuesta basura
    no significan que Ollama esté caído, y re-pingear en bucle por ellos no aporta.
    """
    try:
        from openai import APIConnectionError, APITimeoutError

        return isinstance(exc, (APIConnectionError, APITimeoutError))
    except Exception:  # noqa: BLE001 - sin openai instalado no clasificamos (best-effort)
        return False


def _invalidate_available() -> None:
    """Fuerza una re-prueba de disponibilidad en la próxima llamada (tras un fallo de red)."""
    global _avail_probe_ts
    _avail_probe_ts = None
    _clear_router_available()


def available() -> bool:
    """``True`` si el LLM local está habilitado y Ollama responde.

    A diferencia del ping one-shot del router (pensado para la sesión corta de voz), aquí el ping se
    re-prueba cada ``_AVAIL_PROBE_TTL_S`` invalidando su lru_cache: en un plan largo Ollama puede
    caerse o arrancar tarde, y congelar la decisión del primer paso haría que cada paso local pagara
    el timeout antes de caer a Claude (o que nunca se usara local aunque ya esté arriba).
    """
    if not config.LOCAL_LLM_ENABLED:
        return False
    global _avail_probe_ts
    now = time.monotonic()
    if _avail_probe_ts is None or (now - _avail_probe_ts) >= _AVAIL_PROBE_TTL_S:
        _clear_router_available()  # re-ping fresco; router._available lo re-cachea al llamarlo
        _avail_probe_ts = now
    return bool(router._available())


def complete(
    prompt: str,
    system: str | None = None,
    *,
    model: str | None = None,
    keep_alive: str | None = None,
    timeout: float | None = None,
    observe: bool = True,
) -> str | None:
    """Completa un prompt con el LLM local in-process. Devuelve la respuesta limpia o ``None``.

    Pensado para pasos de razonamiento/lectura del orquestador (no para la voz, que usa
    :func:`agent.router.ask_local` y se queda siempre en el modelo ligero por latencia).

    Args:
        prompt: mensaje del usuario/tarea.
        system: system prompt; ``None`` usa :func:`agent.router.build_system_local`.
        model: id del modelo local; ``None`` => :func:`select_local_model` (auto por hardware).
        keep_alive: valor ``keep_alive`` de Ollama; ``None`` => dinámico (0 si la voz compite).
        timeout: tope duro en segundos; al superarlo devuelve ``None`` (el caller cae a Claude).
            ``None`` => :data:`config.LOCAL_LLM_TIMEOUT_S` (configurable, default 60 s).
        observe: si ``True``, notifica al observer de costos con ``mode="local"`` (tokens locales,
            coste 0 frente al budget de Claude).

    Returns:
        Texto de la respuesta (sin bloques ``<think>``), o ``None`` ante CUALQUIER fallo
        (red caída, timeout, respuesta vacía). NUNCA lanza.
    """
    mdl = model or select_local_model()
    ka = keep_alive if keep_alive is not None else _keep_alive()
    to = timeout if timeout is not None else float(config.LOCAL_LLM_TIMEOUT_S)
    sys_prompt = system if system is not None else router.build_system_local()
    try:
        from openai import OpenAI

        client = OpenAI(base_url=config.LOCAL_LLM_BASE_URL, api_key="ollama", timeout=to)
        resp = client.chat.completions.create(
            model=mdl,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": prompt},
            ],
            extra_body={"keep_alive": ka},
        )
        out = router._strip_think(resp.choices[0].message.content or "")
    except Exception as exc:  # noqa: BLE001 - best-effort; cualquier fallo -> fallback a Claude
        # Si Ollama no respondió a nivel de RED (caído/colgado), invalida la disponibilidad para
        # re-probar en el próximo paso en vez de reintentar local (y pagar el timeout) cada vez.
        if _es_error_de_conexion(exc):
            _invalidate_available()
        return None
    if not out:
        return None
    if observe:
        from agent import executors

        executors.notify_observer("local", prompt, out)
    return out
