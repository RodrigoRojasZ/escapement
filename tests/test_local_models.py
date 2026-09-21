"""Tests del backend local in-process + auto-selección por hardware (F1, Clase A).

Aislados: sin Ollama, sin red, sin GPU. Se mockean ``subprocess.run`` (nvidia-smi), el cliente
OpenAI y el estado de la voz. Se invalida el cache de VRAM entre casos con ``reset_cache()``.
"""

import json
import time
import types

import pytest

from agent import local_models


@pytest.fixture(autouse=True)
def _limpia_cache():
    local_models.reset_cache()
    yield
    local_models.reset_cache()


# --- free_vram_mb --------------------------------------------------------------


def _fake_smi(stdout="6000\n", returncode=0):
    def _run(*a, **k):
        return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    return _run


def test_free_vram_parsea_la_primera_linea(monkeypatch):
    monkeypatch.setattr(local_models.subprocess, "run", _fake_smi("6000\n1234\n"))
    assert local_models.free_vram_mb() == 6000  # GPU 0 = primera línea


def test_free_vram_timeout_es_none(monkeypatch):
    def _boom(*a, **k):
        raise local_models.subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=3.0)

    monkeypatch.setattr(local_models.subprocess, "run", _boom)
    assert local_models.free_vram_mb() is None  # sin lectura fiable -> None (=> VRAM baja)


def test_free_vram_sin_driver_es_none(monkeypatch):
    def _oserror(*a, **k):
        raise OSError("nvidia-smi no está en PATH")

    monkeypatch.setattr(local_models.subprocess, "run", _oserror)
    assert local_models.free_vram_mb() is None


def test_free_vram_returncode_no_cero_es_none(monkeypatch):
    monkeypatch.setattr(local_models.subprocess, "run", _fake_smi("error", returncode=1))
    assert local_models.free_vram_mb() is None


def test_free_vram_cachea(monkeypatch):
    llamadas = []

    def _run(*a, **k):
        llamadas.append(1)
        return types.SimpleNamespace(returncode=0, stdout="6000\n", stderr="")

    monkeypatch.setattr(local_models.subprocess, "run", _run)
    local_models.free_vram_mb()
    local_models.free_vram_mb()
    assert len(llamadas) == 1  # segunda lectura sale del cache (no re-shellea nvidia-smi)


# --- voice_recently_active -----------------------------------------------------


def test_voice_recently_active_sin_archivo_es_falso(monkeypatch, tmp_path):
    monkeypatch.setattr(local_models.config, "DATA_DIR", tmp_path)
    assert local_models.voice_recently_active() is False


def test_voice_recently_active_reciente_es_verdadero(monkeypatch, tmp_path):
    import time

    monkeypatch.setattr(local_models.config, "DATA_DIR", tmp_path)
    (tmp_path / "voice_session.json").write_text(
        json.dumps({"last_activity": time.time()}), encoding="utf-8"
    )
    assert local_models.voice_recently_active() is True


def test_voice_recently_active_vieja_es_falso(monkeypatch, tmp_path):
    import time

    monkeypatch.setattr(local_models.config, "DATA_DIR", tmp_path)
    (tmp_path / "voice_session.json").write_text(
        json.dumps({"last_activity": time.time() - 10_000}), encoding="utf-8"
    )
    assert local_models.voice_recently_active() is False  # fuera de la ventana (600s)


# --- select_local_model (la matriz de decisión) --------------------------------


def test_select_auto_off_siempre_ligero(monkeypatch):
    monkeypatch.setattr(local_models.config, "LOCAL_AUTO_SELECT", False)
    monkeypatch.setattr(local_models.config, "LOCAL_LLM_MODEL", "ligero")
    monkeypatch.setattr(local_models.config, "LOCAL_LLM_MODEL_MEDIUM", "medio")
    assert local_models.select_local_model() == "ligero"


def test_select_voz_activa_fuerza_ligero(monkeypatch):
    monkeypatch.setattr(local_models.config, "LOCAL_AUTO_SELECT", True)
    monkeypatch.setattr(local_models.config, "LOCAL_LLM_MODEL", "ligero")
    monkeypatch.setattr(local_models.config, "LOCAL_LLM_MODEL_MEDIUM", "medio")
    monkeypatch.setattr(local_models.config, "voice_session_active", lambda: True)
    monkeypatch.setattr(local_models, "free_vram_mb", lambda: 8000)  # aunque sobre VRAM
    assert local_models.select_local_model() == "ligero"


def test_select_vram_baja_es_ligero(monkeypatch):
    monkeypatch.setattr(local_models.config, "LOCAL_AUTO_SELECT", True)
    monkeypatch.setattr(local_models.config, "LOCAL_LLM_MODEL", "ligero")
    monkeypatch.setattr(local_models.config, "LOCAL_LLM_MODEL_MEDIUM", "medio")
    monkeypatch.setattr(local_models.config, "LOCAL_VRAM_MEDIUM_MB", 5600)
    monkeypatch.setattr(local_models.config, "voice_session_active", lambda: False)
    monkeypatch.setattr(local_models, "voice_recently_active", lambda: False)
    monkeypatch.setattr(local_models, "free_vram_mb", lambda: 3000)
    assert local_models.select_local_model() == "ligero"


def test_select_vram_none_es_ligero(monkeypatch):
    monkeypatch.setattr(local_models.config, "LOCAL_AUTO_SELECT", True)
    monkeypatch.setattr(local_models.config, "LOCAL_LLM_MODEL", "ligero")
    monkeypatch.setattr(local_models.config, "LOCAL_LLM_MODEL_MEDIUM", "medio")
    monkeypatch.setattr(local_models.config, "voice_session_active", lambda: False)
    monkeypatch.setattr(local_models, "voice_recently_active", lambda: False)
    monkeypatch.setattr(local_models, "free_vram_mb", lambda: None)  # sin lectura -> conservador
    assert local_models.select_local_model() == "ligero"


def _setup_medio_alcanzable(monkeypatch):
    """VRAM holgada + voz off: el único factor que decide ligero/medio es si el MEDIO está pulled."""
    monkeypatch.setattr(local_models.config, "LOCAL_AUTO_SELECT", True)
    monkeypatch.setattr(local_models.config, "LOCAL_LLM_MODEL", "ligero")
    monkeypatch.setattr(local_models.config, "LOCAL_LLM_MODEL_MEDIUM", "medio")
    monkeypatch.setattr(local_models.config, "LOCAL_VRAM_MEDIUM_MB", 5600)
    monkeypatch.setattr(local_models.config, "voice_session_active", lambda: False)
    monkeypatch.setattr(local_models, "voice_recently_active", lambda: False)
    monkeypatch.setattr(local_models, "free_vram_mb", lambda: 7000)


def test_select_vram_holgada_y_voz_off_sube_a_medio(monkeypatch):
    _setup_medio_alcanzable(monkeypatch)
    monkeypatch.setattr(local_models, "_pulled_models", lambda: frozenset({"medio"}))
    assert local_models.select_local_model() == "medio"


def test_select_medio_no_instalado_degrada_a_ligero(monkeypatch):
    # VRAM y voz permitirían el medio, pero no está pulled -> ligero (evita 404ear cada paso).
    _setup_medio_alcanzable(monkeypatch)
    monkeypatch.setattr(local_models, "_pulled_models", lambda: frozenset({"otro:7b"}))
    assert local_models.select_local_model() == "ligero"


def test_select_tags_no_verificable_es_ligero(monkeypatch):
    # /api/tags no respondió (set vacío): "no verificable" se trata como "ausente" -> ligero.
    _setup_medio_alcanzable(monkeypatch)
    monkeypatch.setattr(local_models, "_pulled_models", lambda: frozenset())
    assert local_models.select_local_model() == "ligero"


# --- _pulled_models / _esta_instalado ------------------------------------------


def test_esta_instalado_exacto_y_latest():
    assert local_models._esta_instalado("qwen2.5:3b", frozenset({"qwen2.5:3b"})) is True
    # tag :latest implícito: el modelo sin ':' matchea contra '<mdl>:latest'
    assert local_models._esta_instalado("qwen", frozenset({"qwen:latest"})) is True
    assert local_models._esta_instalado("qwen2.5:3b", frozenset({"otro"})) is False
    # con tag explícito NO se asume :latest
    assert local_models._esta_instalado("qwen2.5:3b", frozenset({"qwen2.5:latest"})) is False


def _fake_tags(monkeypatch, models, contador=None):
    """Mockea httpx.get para /api/tags devolviendo {"models": [...]}. Cuenta llamadas si se pide."""
    import httpx

    def _get(*a, **k):
        if contador is not None:
            contador.append(1)
        return types.SimpleNamespace(json=lambda: {"models": models})

    monkeypatch.setattr(httpx, "get", _get)


def test_pulled_models_parsea_nombres(monkeypatch):
    _fake_tags(monkeypatch, [{"name": "qwen2.5:3b"}, {"name": "qwen2.5-coder:7b"}, {"sin": "name"}])
    assert local_models._pulled_models() == frozenset({"qwen2.5:3b", "qwen2.5-coder:7b"})


def test_pulled_models_error_es_vacio(monkeypatch):
    import httpx

    def _boom(*a, **k):
        raise RuntimeError("sin Ollama")

    monkeypatch.setattr(httpx, "get", _boom)
    assert local_models._pulled_models() == frozenset()  # NUNCA lanza -> set vacío


def test_pulled_models_cachea(monkeypatch):
    contador = []
    _fake_tags(monkeypatch, [{"name": "qwen2.5:3b"}], contador=contador)
    local_models._pulled_models()
    local_models._pulled_models()
    assert len(contador) == 1  # 2ª sale del cache
    local_models.reset_cache()
    local_models._pulled_models()
    assert len(contador) == 2  # tras reset se re-consulta


# --- available -----------------------------------------------------------------


def test_available_requiere_enabled_y_ollama(monkeypatch):
    monkeypatch.setattr(local_models.config, "LOCAL_LLM_ENABLED", True)
    monkeypatch.setattr(local_models.router, "_available", lambda: True)
    assert local_models.available() is True
    monkeypatch.setattr(local_models.config, "LOCAL_LLM_ENABLED", False)
    assert local_models.available() is False
    monkeypatch.setattr(local_models.config, "LOCAL_LLM_ENABLED", True)
    monkeypatch.setattr(local_models.router, "_available", lambda: False)
    assert local_models.available() is False


def _ping_contador():
    """Fake de router._available que cuenta pings y expone cache_clear (como el lru_cache real)."""
    estado = {"clear": 0, "ping": 0}

    def _ping():
        estado["ping"] += 1
        return True

    _ping.cache_clear = lambda: estado.__setitem__("clear", estado["clear"] + 1)
    return _ping, estado


def test_available_reprueba_ollama_tras_el_ttl(monkeypatch):
    # El orquestador corre planes largos: la disponibilidad NO se congela como en el router one-shot.
    ping, estado = _ping_contador()
    monkeypatch.setattr(local_models.config, "LOCAL_LLM_ENABLED", True)
    monkeypatch.setattr(local_models.router, "_available", ping)
    # 1ª llamada (probe_ts None): fuerza un re-ping fresco (limpia el lru_cache del router).
    assert local_models.available() is True
    assert estado["clear"] == 1
    # 2ª llamada inmediata (dentro del TTL): NO vuelve a limpiar; sirve el cache del router.
    assert local_models.available() is True
    assert estado["clear"] == 1
    # pasado el TTL, re-prueba de nuevo.
    local_models._avail_probe_ts = time.monotonic() - (local_models._AVAIL_PROBE_TTL_S + 1)
    assert local_models.available() is True
    assert estado["clear"] == 2


def test_available_deshabilitado_no_pingea(monkeypatch):
    # enabled=False corta ANTES de tocar Ollama (ni ping ni clear): apagado total y barato.
    ping, estado = _ping_contador()
    monkeypatch.setattr(local_models.config, "LOCAL_LLM_ENABLED", False)
    monkeypatch.setattr(local_models.router, "_available", ping)
    assert local_models.available() is False
    assert estado["ping"] == 0 and estado["clear"] == 0


def test_es_error_de_conexion_distingue_red_de_404():
    import httpx
    import openai

    req = httpx.Request("POST", "http://localhost:11434/v1/chat/completions")
    assert local_models._es_error_de_conexion(openai.APIConnectionError(request=req)) is True
    assert local_models._es_error_de_conexion(openai.APITimeoutError(request=req)) is True
    assert local_models._es_error_de_conexion(RuntimeError("modelo 404")) is False


# --- complete (nunca lanza; observer local) ------------------------------------


class _FakeClient:
    def __init__(self, content):
        self._content = content
        msg = types.SimpleNamespace(content=content)
        choice = types.SimpleNamespace(message=msg)
        self._resp = types.SimpleNamespace(choices=[choice])

    class _Chat:
        pass

    def _make(self):
        chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=lambda **k: self._resp)
        )
        return chat


def _patch_openai(monkeypatch, content, capture=None):
    def _factory(*a, **k):
        client = _FakeClient(content)
        client.chat = client._make()
        if capture is not None:
            capture["kwargs"] = k
        return client

    import openai

    monkeypatch.setattr(openai, "OpenAI", _factory)


def test_complete_devuelve_texto_limpio(monkeypatch):
    monkeypatch.setattr(local_models, "select_local_model", lambda: "ligero")
    _patch_openai(monkeypatch, "<think>razono</think>respuesta")
    out = local_models.complete("hola", system="s", observe=False)
    assert out == "respuesta"  # _strip_think aplicado


def test_complete_vacio_es_none(monkeypatch):
    monkeypatch.setattr(local_models, "select_local_model", lambda: "ligero")
    _patch_openai(monkeypatch, "")
    assert local_models.complete("hola", system="s", observe=False) is None


def test_complete_excepcion_es_none(monkeypatch):
    monkeypatch.setattr(local_models, "select_local_model", lambda: "ligero")

    def _boom(*a, **k):
        raise RuntimeError("ollama caído")

    import openai

    monkeypatch.setattr(openai, "OpenAI", _boom)
    assert local_models.complete("hola", system="s", observe=False) is None  # NUNCA lanza


def test_complete_notifica_observer_con_mode_local(monkeypatch):
    monkeypatch.setattr(local_models, "select_local_model", lambda: "ligero")
    _patch_openai(monkeypatch, "salida")
    llamadas = []
    from agent import executors

    monkeypatch.setattr(executors, "notify_observer", lambda m, p, o: llamadas.append((m, p, o)))
    out = local_models.complete("prompt", system="s", observe=True)
    assert out == "salida"
    assert llamadas == [("local", "prompt", "salida")]


def _boom_openai(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("ollama sin red")

    import openai

    monkeypatch.setattr(openai, "OpenAI", _boom)


def test_complete_error_de_conexion_invalida_disponibilidad(monkeypatch):
    # Ollama caído a mitad del plan: se fuerza re-prueba (no reintentar local pagando el timeout).
    monkeypatch.setattr(local_models, "select_local_model", lambda: "ligero")
    monkeypatch.setattr(local_models, "_es_error_de_conexion", lambda e: True)
    _boom_openai(monkeypatch)
    local_models._avail_probe_ts = 999.0  # "ya probado hace poco"
    assert local_models.complete("hola", system="s", observe=False) is None
    assert local_models._avail_probe_ts is None  # invalidado -> re-prueba en el próximo paso


def test_complete_error_no_de_conexion_no_invalida(monkeypatch):
    # Un 404 (modelo ausente) o basura del modelo NO significa Ollama caído: no re-pingear en bucle.
    monkeypatch.setattr(local_models, "select_local_model", lambda: "ligero")
    monkeypatch.setattr(local_models, "_es_error_de_conexion", lambda e: False)
    _boom_openai(monkeypatch)
    local_models._avail_probe_ts = 999.0
    assert local_models.complete("hola", system="s", observe=False) is None
    assert local_models._avail_probe_ts == 999.0  # intacto


def test_complete_timeout_sale_de_config_por_defecto(monkeypatch):
    monkeypatch.setattr(local_models, "select_local_model", lambda: "ligero")
    monkeypatch.setattr(local_models.config, "LOCAL_LLM_TIMEOUT_S", 33)
    cap = {}
    _patch_openai(monkeypatch, "ok", capture=cap)
    local_models.complete("hola", system="s", observe=False)
    assert cap["kwargs"]["timeout"] == 33.0  # None => config.LOCAL_LLM_TIMEOUT_S


def test_complete_timeout_explicito_gana(monkeypatch):
    monkeypatch.setattr(local_models, "select_local_model", lambda: "ligero")
    monkeypatch.setattr(local_models.config, "LOCAL_LLM_TIMEOUT_S", 33)
    cap = {}
    _patch_openai(monkeypatch, "ok", capture=cap)
    local_models.complete("hola", system="s", timeout=5, observe=False)
    assert cap["kwargs"]["timeout"] == 5  # el override explícito pisa el default de config
