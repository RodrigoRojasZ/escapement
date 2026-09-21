from importlib import reload

import agent.config as config_module
import agent.router as router_module

config = reload(config_module)
router = reload(router_module)


def test_system_local_contextualiza_identidad_y_limitaciones():
    prompt = str(router.SYSTEM_LOCAL)

    assert config.AGENT_NAME in prompt
    assert config.USER_NAME in prompt  # quien diga la config; sin config, "tu usuario"
    assert "sin markdown" in prompt.lower()
    assert "no inventes" in prompt.lower()
    assert "modo online" in prompt.lower()


# --- Heurística de routing (P1b: en duda -> online). _classify no depende de Ollama. ---


def test_fuerza_local_explicito():
    assert router._classify("resuélvelo en local: dame un sinónimo de rápido") == "local"


def test_fuerza_online_explicito():
    assert router._classify("usa claude para esto") == "claude"


def test_razonamiento_sustancial_va_a_claude():
    assert router._classify("analiza los pros y contras de esta arquitectura") == "claude"


def test_tools_o_accion_va_a_claude():
    assert router._classify("ejecuta el scraper de coppel") == "claude"


def test_turno_largo_en_duda_va_a_claude():
    # >40 palabras sin señales claras: en duda -> online (prioriza calidad).
    assert router._classify("necesito ayuda con una cosa " * 10) == "claude"


def test_trivial_corto_va_a_local():
    assert router._classify("hola, buenos días") == "local"


# --- Clasificador LLM de respaldo + sticky routing (arreglan la parálisis del local). ---


def test_is_followup_detecta_continuaciones():
    assert router._is_followup("prosigue")
    assert router._is_followup("sí")
    assert router._is_followup("dale")
    assert not router._is_followup("evalúa la documentación del repositorio completo por favor")


def test_route_sticky_followup_se_queda_en_claude(monkeypatch):
    monkeypatch.setattr(router, "_available", lambda: True)
    llamado = []
    monkeypatch.setattr(router, "_classify_llm", lambda t: llamado.append(1) or "local")
    assert router.route("prosigue", last_backend="claude") == "claude"
    assert not llamado  # el sticky decidió sin invocar al clasificador


def test_route_falso_negativo_del_heuristico_va_al_clasificador(monkeypatch):
    monkeypatch.setattr(router, "_available", lambda: True)
    monkeypatch.setattr(router, "_classify_llm", lambda t: "claude")
    # "evalúes...documentación" no dispara ninguna regex -> heurístico 'local' -> clasificador
    assert router.route("necesito que evalúes la documentación del repositorio") == "claude"


def test_classify_llm_charla_es_local(monkeypatch):
    monkeypatch.setattr(router, "ask_local", lambda text, system_prompt=None: "charla")
    assert router._classify_llm("hola qué tal") == "local"


def test_classify_llm_accion_es_claude(monkeypatch):
    monkeypatch.setattr(router, "ask_local", lambda text, system_prompt=None: "accion")
    assert router._classify_llm("edita el config") == "claude"


# --- _is_thinking_model + /no_think condicional (qwen3 razona, qwen2.5 no entiende el token) ---


def test_is_thinking_model_detecta_qwen3():
    assert router._is_thinking_model("qwen3:4b")
    assert not router._is_thinking_model("qwen2.5:3b")
    assert not router._is_thinking_model("qwen2.5-coder:7b")
    assert not router._is_thinking_model("")


def test_no_think_suffix_condicional():
    assert router._no_think_suffix("qwen3:4b") == " /no_think"
    assert router._no_think_suffix("qwen2.5:3b") == ""


def test_no_think_no_se_anexa_con_qwen25(monkeypatch):
    monkeypatch.setattr(router.config, "LOCAL_LLM_MODEL", "qwen2.5:3b")
    assert "/no_think" not in str(router.build_system_local())
    capturado = {}

    def fake_ask(text, system_prompt=None):
        capturado["sys"] = system_prompt
        return "charla"

    monkeypatch.setattr(router, "ask_local", fake_ask)
    router._classify_llm("hola")
    assert "/no_think" not in capturado["sys"]


def test_no_think_se_anexa_con_qwen3(monkeypatch):
    monkeypatch.setattr(router.config, "LOCAL_LLM_MODEL", "qwen3:4b")
    assert str(router.build_system_local()).rstrip().endswith("/no_think")
