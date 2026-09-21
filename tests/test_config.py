from importlib import reload
from pathlib import Path

import pytest


@pytest.fixture
def config_module(monkeypatch, tmp_path):
    # hermético: que el reload no lea un escapement.toml real de la máquina (M1)
    monkeypatch.setenv("AGENT_CONFIG_FILE", str(tmp_path / "no-existe.toml"))
    monkeypatch.setenv("AGENT_NAME", "Mina")
    monkeypatch.setenv("AGENT_PROJECTS_DIR", "/tmp/agent-projects")
    monkeypatch.setenv("AGENT_RECALL_SCRIPT", "/tmp/recall.py")

    import agent.config as config

    return reload(config)


def test_config_usa_agente_y_env_nuevas(config_module):
    assert config_module.AGENT_NAME == "Mina"
    assert config_module.AGENT_SLUG == "mina"
    assert config_module.PROJECTS_DIR == Path("/tmp/agent-projects")
    assert config_module.RECALL_SCRIPT == Path("/tmp/recall.py")
    # El home del agente cuelga del slug de SU RUTA, no de su nombre: Claude Code deriva la
    # carpeta del vault del cwd de la sesión, así que un caso especial mandaría las notas a
    # una carpeta que ese índice nunca carga (ver config.vault_slug).
    assert config_module.AGENT_HOME == Path("/tmp/agent-projects") / config_module.vault_slug(
        str(config_module.REPO_ROOT)
    )
    # ledger/conversaciones ya no cuelgan de AGENT_HOME sino de DATA_DIR (dentro del repo).
    assert config_module.LEDGER == config_module.DATA_DIR / "ledger.jsonl"
    assert config_module.CONVERSATIONS == config_module.DATA_DIR / "conversations.jsonl"


def test_data_dir_respeta_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CONFIG_FILE", str(tmp_path / "no-existe.toml"))  # hermético (M1)
    monkeypatch.setenv("AGENT_DATA_DIR", "/tmp/vdata")
    import agent.config as config

    cfg = reload(config)
    assert cfg.DATA_DIR == Path("/tmp/vdata")
    assert cfg.LEDGER == Path("/tmp/vdata/ledger.jsonl")
    monkeypatch.delenv("AGENT_DATA_DIR")
    reload(config)  # restaura el módulo real para el resto de la suite


def test_vault_slug_deriva_carpeta_del_repo():
    import agent.config as config

    # ruta de repo -> slug con no-alfanuméricos vueltos '-' (misma convención que Claude Code)
    assert config.vault_slug(r"D:\mi_proyecto").endswith("mi-proyecto")
    assert "/" not in config.vault_slug("/home/x/mi_repo")
    # SIN excepciones: el propio repo del agente también deriva su slug de la ruta, para caer
    # en la misma carpeta que abre Claude Code cuando la sesión arranca en ese cwd.
    assert config.vault_slug(str(config.REPO_ROOT)) != config.AGENT_SLUG
    assert config.vault_slug(str(config.REPO_ROOT)).endswith(
        config.REPO_ROOT.name.replace("_", "-")
    )


# --- M1: config externalizada en escapement.toml (env > archivo > default) ---


def _config_fresca(monkeypatch, tmp_path, toml_text=None, env=None):
    """Copia FRESCA del módulo con el env dado, sin recargar el ``agent.config`` global.

    Un reload del global contaminaría a los ~20 módulos que ya lo importaron; ejecutar el
    archivo como módulo aparte prueba la lógica de import-time en aislamiento. Siempre fija
    AGENT_CONFIG_FILE (hermético: nunca lee un escapement.toml real de la máquina).
    """
    import importlib.util

    import agent.config as config

    archivo = tmp_path / "escapement.toml"
    if toml_text is not None:
        archivo.write_text(toml_text, encoding="utf-8")
    monkeypatch.setenv("AGENT_CONFIG_FILE", str(archivo))
    for var in (
        "AGENT_DATA_DIR",
        "AGENT_RECALL_SCRIPT",
        "AGENT_PROJECTS_DIR",
        "AGENT_USER_NAME",
        "AGENT_USER_PROFILE",
    ):
        monkeypatch.delenv(var, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("agent_config_fresca", config.__file__)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_m1_sin_archivo_mantiene_defaults(monkeypatch, tmp_path):
    c = _config_fresca(monkeypatch, tmp_path)  # AGENT_CONFIG_FILE apunta a un toml inexistente
    assert c.REPOS == c._DEFAULT_REPOS  # sin archivo = comportamiento previo EXACTO
    assert list(c.REPOS) == ["escapement"]  # el único repo por default es el del propio agente
    assert c.DATA_DIR == c.REPO_ROOT / "data"


def test_m1_repos_del_archivo_agregan_y_sobreescriben(monkeypatch, tmp_path):
    c = _config_fresca(
        monkeypatch,
        tmp_path,
        toml_text='[repos]\nmi_proyecto = "D:/mi_proyecto"\notro = "D:/otro"\n',
    )
    assert c.REPOS["mi_proyecto"] == "D:/mi_proyecto"  # nuevo
    assert c.REPOS["otro"] == "D:/otro"  # nuevo
    assert c.REPOS["escapement"] == str(c.REPO_ROOT)  # default no pisado sobrevive


def test_m1_repos_del_archivo_pueden_pisar_el_default(monkeypatch, tmp_path):
    c = _config_fresca(monkeypatch, tmp_path, toml_text='[repos]\nescapement = "D:/fork"\n')
    assert c.REPOS == {"escapement": "D:/fork"}  # el archivo gana sobre _DEFAULT_REPOS


def test_m1_rutas_del_archivo_se_respetan(monkeypatch, tmp_path):
    c = _config_fresca(
        monkeypatch,
        tmp_path,
        toml_text=(
            f'data_dir = "{tmp_path.as_posix()}/datos"\n'
            f'projects_dir = "{tmp_path.as_posix()}/proyectos"\n'
            f'recall_script = "{tmp_path.as_posix()}/recall.py"\n'
        ),
    )
    assert c.DATA_DIR == tmp_path / "datos"
    assert c.PROJECTS_DIR == tmp_path / "proyectos"
    assert c.RECALL_SCRIPT == tmp_path / "recall.py"
    assert c.LEDGER == tmp_path / "datos" / "ledger.jsonl"  # los derivados siguen a DATA_DIR


def test_m1_env_var_gana_sobre_el_archivo(monkeypatch, tmp_path):
    c = _config_fresca(
        monkeypatch,
        tmp_path,
        toml_text=f'data_dir = "{tmp_path.as_posix()}/del-archivo"\n',
        env={"AGENT_DATA_DIR": str(tmp_path / "del-env")},
    )
    assert c.DATA_DIR == tmp_path / "del-env"


def test_m1_archivo_corrupto_cae_a_defaults(monkeypatch, tmp_path):
    c = _config_fresca(monkeypatch, tmp_path, toml_text="esto no es [toml valido")
    assert c.REPOS == c._DEFAULT_REPOS  # best-effort: no revienta el import
    assert c.DATA_DIR == c.REPO_ROOT / "data"


# --- STT ligero: VOICE_STT_* configurables (env > [voice] toml > default) ---


def _sin_voice_env(monkeypatch):
    for var in (
        "AGENT_VOICE_STT_MODEL",
        "AGENT_VOICE_STT_DEVICE",
        "AGENT_VOICE_STT_COMPUTE",
        "AGENT_VOICE_WAKE_WORD",
        "AGENT_VOICE_WAKE_THRESHOLD",
        "AGENT_VOICE_PTT_MODE",
        "AGENT_VOICE_BARGE_IN",
    ):
        monkeypatch.delenv(var, raising=False)


def test_voice_stt_defaults_sin_archivo(monkeypatch, tmp_path):
    _sin_voice_env(monkeypatch)
    c = _config_fresca(monkeypatch, tmp_path)
    assert c.VOICE_STT_MODEL == "large-v3-turbo"  # multilingüe es/en, ~1.2 GB VRAM
    assert c.VOICE_STT_DEVICE == "cuda"
    assert c.VOICE_STT_COMPUTE == "int8_float16"  # deja margen de VRAM: float16 desborda en 8 GB


def test_voice_stt_toml_override(monkeypatch, tmp_path):
    _sin_voice_env(monkeypatch)
    c = _config_fresca(
        monkeypatch,
        tmp_path,
        toml_text='[voice]\nstt_model = "large-v3"\nstt_compute = "float16"\n',
    )
    assert c.VOICE_STT_MODEL == "large-v3"  # rollback al modelo previo vía archivo
    assert c.VOICE_STT_COMPUTE == "float16"  # distinto del default: prueba que el archivo manda
    assert c.VOICE_STT_DEVICE == "cuda"  # clave ausente en [voice] conserva el default


def test_voice_stt_env_gana_sobre_toml(monkeypatch, tmp_path):
    _sin_voice_env(monkeypatch)
    c = _config_fresca(
        monkeypatch,
        tmp_path,
        toml_text='[voice]\nstt_model = "del-archivo"\n',
        env={"AGENT_VOICE_STT_MODEL": "del-env", "AGENT_VOICE_STT_DEVICE": "cpu"},
    )
    assert c.VOICE_STT_MODEL == "del-env"
    assert c.VOICE_STT_DEVICE == "cpu"


def test_voice_stt_valores_se_normalizan_con_strip(monkeypatch, tmp_path):
    _sin_voice_env(monkeypatch)
    c = _config_fresca(monkeypatch, tmp_path, env={"AGENT_VOICE_STT_MODEL": "  medium  "})
    assert c.VOICE_STT_MODEL == "medium"


def test_voice_stt_env_vacia_es_valor_no_default(monkeypatch, tmp_path):
    # misma semántica que _local_str: una env presente (aunque vacía) ES el valor; "" != ausente
    _sin_voice_env(monkeypatch)
    c = _config_fresca(monkeypatch, tmp_path, env={"AGENT_VOICE_STT_COMPUTE": ""})
    assert c.VOICE_STT_COMPUTE == ""


# --- Wake word (manos libres): VOICE_WAKE_* (env > [voice] toml > default OFF) ---


def test_voice_wake_apagada_por_default(monkeypatch, tmp_path):
    _sin_voice_env(monkeypatch)
    c = _config_fresca(monkeypatch, tmp_path)
    assert c.VOICE_WAKE_WORD == ""  # OFF: el daemon queda en push-to-talk clásico
    assert c.VOICE_WAKE_THRESHOLD == 0.5


def test_voice_wake_toml_override(monkeypatch, tmp_path):
    _sin_voice_env(monkeypatch)
    c = _config_fresca(
        monkeypatch,
        tmp_path,
        toml_text='[voice]\nwake_word = "hey_jarvis"\nwake_threshold = 0.7\n',
    )
    assert c.VOICE_WAKE_WORD == "hey_jarvis"
    assert c.VOICE_WAKE_THRESHOLD == 0.7


def test_voice_wake_env_gana_sobre_toml(monkeypatch, tmp_path):
    _sin_voice_env(monkeypatch)
    c = _config_fresca(
        monkeypatch,
        tmp_path,
        toml_text='[voice]\nwake_word = "alexa"\n',
        env={"AGENT_VOICE_WAKE_WORD": "hey_jarvis", "AGENT_VOICE_WAKE_THRESHOLD": "0.8"},
    )
    assert c.VOICE_WAKE_WORD == "hey_jarvis"
    assert c.VOICE_WAKE_THRESHOLD == 0.8


def test_voice_wake_threshold_invalido_usa_default(monkeypatch, tmp_path):
    # un float que no parsea no debe tumbar el import de config: cae al default
    _sin_voice_env(monkeypatch)
    c = _config_fresca(monkeypatch, tmp_path, env={"AGENT_VOICE_WAKE_THRESHOLD": "alto"})
    assert c.VOICE_WAKE_THRESHOLD == 0.5


# --- Modo de la hotkey (toggle/hold): VOICE_PTT_MODE (env > [voice] toml > default toggle) ---


def test_voice_ptt_mode_default_toggle(monkeypatch, tmp_path):
    _sin_voice_env(monkeypatch)
    c = _config_fresca(monkeypatch, tmp_path)
    assert c.VOICE_PTT_MODE == "toggle"
    assert c.voice_ptt_verbo() == "presiona"


def test_voice_ptt_mode_hold_por_toml(monkeypatch, tmp_path):
    _sin_voice_env(monkeypatch)
    c = _config_fresca(monkeypatch, tmp_path, toml_text='[voice]\nptt_mode = "hold"\n')
    assert c.VOICE_PTT_MODE == "hold"
    assert c.voice_ptt_verbo() == "mantén"


def test_voice_ptt_mode_env_gana_sobre_toml(monkeypatch, tmp_path):
    _sin_voice_env(monkeypatch)
    c = _config_fresca(
        monkeypatch,
        tmp_path,
        toml_text='[voice]\nptt_mode = "hold"\n',
        env={"AGENT_VOICE_PTT_MODE": "toggle"},
    )
    assert c.VOICE_PTT_MODE == "toggle"


def test_voice_ptt_mode_invalido_cae_a_toggle(monkeypatch, tmp_path):
    # un typo en escapement.toml no debe tumbar el arranque ni dejar un modo desconocido
    _sin_voice_env(monkeypatch)
    c = _config_fresca(monkeypatch, tmp_path, env={"AGENT_VOICE_PTT_MODE": "apretar"})
    assert c.VOICE_PTT_MODE == "toggle"


def test_voice_ptt_mode_normaliza_mayusculas_y_espacios(monkeypatch, tmp_path):
    _sin_voice_env(monkeypatch)
    c = _config_fresca(monkeypatch, tmp_path, env={"AGENT_VOICE_PTT_MODE": "  HOLD "})
    assert c.VOICE_PTT_MODE == "hold"


# --- Barge-in: VOICE_BARGE_IN configurable (env > [voice] toml > default ON) ---


def test_voice_barge_in_encendido_por_default(monkeypatch, tmp_path):
    _sin_voice_env(monkeypatch)
    c = _config_fresca(monkeypatch, tmp_path)
    assert c.VOICE_BARGE_IN is True  # sin env ni archivo = comportamiento previo EXACTO


def test_voice_barge_in_apagable_por_toml(monkeypatch, tmp_path):
    # micrófono sensible / altavoces abiertos: apagarlo sin editar código (deuda #3)
    _sin_voice_env(monkeypatch)
    c = _config_fresca(monkeypatch, tmp_path, toml_text="[voice]\nbarge_in = false\n")
    assert c.VOICE_BARGE_IN is False


def test_voice_barge_in_env_gana_sobre_toml(monkeypatch, tmp_path):
    _sin_voice_env(monkeypatch)
    c = _config_fresca(
        monkeypatch,
        tmp_path,
        toml_text="[voice]\nbarge_in = false\n",
        env={"AGENT_VOICE_BARGE_IN": "1"},
    )
    assert c.VOICE_BARGE_IN is True


# --- Tiering de modelo por tarea (model_for): env > [modelos] toml > default OFF ---


def _sin_executor_model(monkeypatch):
    # el override global gana sobre el tiering; lo apagamos para probar el tiering en aislamiento
    monkeypatch.delenv("AGENT_EXECUTOR_MODEL", raising=False)


def test_tiering_off_por_defecto_es_noop(monkeypatch, tmp_path):
    _sin_executor_model(monkeypatch)
    c = _config_fresca(monkeypatch, tmp_path)  # sin archivo ni env de tiering
    assert c.MODEL_TIERING is False
    for kind in ("planner", "editar", "investigar", "memoria", "triage", "desconocido"):
        assert c.model_for(kind) == ""  # '' = el CLI elige (comportamiento previo)


def test_tiering_on_por_env_asigna_tiers(monkeypatch, tmp_path):
    _sin_executor_model(monkeypatch)
    c = _config_fresca(monkeypatch, tmp_path, env={"AGENT_MODEL_TIERING": "1"})
    assert c.MODEL_TIERING is True
    assert c.model_for("planner") == c.MODEL_MAIN  # el planner es online medio, sólido (no HEAVY)
    assert c.model_for("editar") == c.MODEL_MAIN
    assert c.model_for("crear") == c.MODEL_MAIN
    assert c.model_for("investigar") == c.MODEL_MAIN
    assert c.model_for("memoria") == c.MODEL_CHEAP
    assert c.model_for("triage") == c.MODEL_CHEAP
    assert c.model_for("desconocido") == c.MODEL_MAIN  # tipo fuera del mapa cae a MAIN


def test_tiering_on_por_toml(monkeypatch, tmp_path):
    _sin_executor_model(monkeypatch)
    c = _config_fresca(monkeypatch, tmp_path, toml_text="[modelos]\ntiering = true\n")
    assert c.MODEL_TIERING is True
    assert c.model_for("memoria") == c.MODEL_CHEAP


def test_tiering_persona_dura_sube_a_heavy(monkeypatch, tmp_path):
    _sin_executor_model(monkeypatch)
    c = _config_fresca(monkeypatch, tmp_path, env={"AGENT_MODEL_TIERING": "1"})
    # un paso 'editar' (MAIN) con persona de razonamiento duro sube a HEAVY
    assert c.model_for("editar", "seguridad") == c.MODEL_HEAVY
    assert c.model_for("investigar", "arquitecto") == c.MODEL_HEAVY
    assert c.model_for("verificar", "Optimizador") == c.MODEL_HEAVY  # case-insensitive
    # una persona no-dura no cambia el tier del tipo
    assert c.model_for("editar", "documentador") == c.MODEL_MAIN


def test_executor_model_override_gana_sobre_tiering(monkeypatch, tmp_path):
    # EXECUTOR_MODEL fija un modelo para TODO dispatch, con o sin tiering
    c = _config_fresca(
        monkeypatch,
        tmp_path,
        env={"AGENT_EXECUTOR_MODEL": "claude-fijo", "AGENT_MODEL_TIERING": "1"},
    )
    assert c.model_for("planner") == "claude-fijo"
    assert c.model_for("memoria", "seguridad") == "claude-fijo"


def test_executor_model_override_gana_con_tiering_off(monkeypatch, tmp_path):
    c = _config_fresca(monkeypatch, tmp_path, env={"AGENT_EXECUTOR_MODEL": "claude-fijo"})
    assert c.MODEL_TIERING is False
    assert c.model_for("editar") == "claude-fijo"  # aunque el tiering esté OFF


# --- Fable desactivado: AGENT_EXECUTOR_MODEL que pida un Fable cae a MODEL_HEAVY ---


def test_fable_pedido_por_env_cae_a_heavy(monkeypatch, tmp_path):
    # Fable está apagado por ahora: pedirlo no despacha Fable, despacha MODEL_HEAVY
    c = _config_fresca(monkeypatch, tmp_path, env={"AGENT_EXECUTOR_MODEL": "claude-fable-5-1"})
    assert c.EXECUTOR_MODEL == c.MODEL_HEAVY == "claude-opus-5"
    assert c.model_for("editar") == "claude-opus-5"  # y el override sigue ganando al tiering


def test_fable_se_detecta_por_prefijo_sin_importar_caja(monkeypatch, tmp_path):
    # cualquier claude-fable-*, no solo el ID exacto de hoy
    c = _config_fresca(
        monkeypatch,
        tmp_path,
        env={"AGENT_EXECUTOR_MODEL": "  Claude-Fable-9-Beta  "},
    )
    assert c.EXECUTOR_MODEL == c.MODEL_HEAVY


def test_modelo_no_fable_pasa_intacto(monkeypatch, tmp_path):
    c = _config_fresca(monkeypatch, tmp_path, env={"AGENT_EXECUTOR_MODEL": "claude-sonnet-5"})
    assert c.EXECUTOR_MODEL == "claude-sonnet-5"  # el filtro solo toca Fable


def test_sin_executor_model_no_fuerza_ningun_modelo(monkeypatch, tmp_path):
    _sin_executor_model(monkeypatch)
    c = _config_fresca(monkeypatch, tmp_path)
    assert c.EXECUTOR_MODEL == ""  # vacío = el default del CLI, no se sustituye por HEAVY


# --- route_step: ruteo estratégico local/online del orquestador (SIEMPRE activo, sin flag) ---


def _orch(monkeypatch, tmp_path, env=None, toml_text=None):
    """Config fresca con Ollama 'disponible' (mock) y voz OFF.

    El ruteo estratégico local/online está SIEMPRE activo (no hay flag de activación), así que
    basta con que el local esté disponible para que los pasos elegibles vayan local.
    """
    from agent import local_models

    monkeypatch.setattr(local_models, "available", lambda: True)
    c = _config_fresca(monkeypatch, tmp_path, toml_text=toml_text, env=env)
    c.set_voice_session(False)
    return c


def test_route_step_sin_local_cae_a_cli_con_modelo_del_tiering(monkeypatch, tmp_path):
    # el ruteo está SIEMPRE activo (sin flag); si el local NO está disponible, cae a cli con el
    # modelo del tiering (comportamiento previo exacto)
    from agent import local_models

    monkeypatch.setattr(local_models, "available", lambda: False)
    c = _config_fresca(monkeypatch, tmp_path)
    c.set_voice_session(False)
    r = c.route_step("reflexionar", accion="lista pasos")
    assert r.backend == "cli" and r.model == c.model_for("reflexionar")


def test_route_step_paso_trivial_va_local(monkeypatch, tmp_path):
    c = _orch(monkeypatch, tmp_path)
    r = c.route_step("reflexionar", accion="lista pasos", done="ok")
    assert r.backend == "local" and r.model == ""


def test_route_step_persona_heavy_fuerza_cli(monkeypatch, tmp_path):
    c = _orch(monkeypatch, tmp_path)
    persona = next(iter(c.HEAVY_PERSONAS))
    assert c.route_step("reflexionar", persona=persona, accion="lista").backend == "cli"


def test_route_step_edicion_nunca_local(monkeypatch, tmp_path):
    c = _orch(monkeypatch, tmp_path)
    for kind in ("editar", "crear", "ejecutar", "planner"):
        assert c.route_step(kind, accion="algo").backend == "cli"


def test_route_step_dificultad_alta_va_cli(monkeypatch, tmp_path):
    c = _orch(monkeypatch, tmp_path)
    # señal de razonamiento sustancial -> cli aunque el tipo sea barato
    r = c.route_step("reflexionar", accion="analiza la arquitectura y compara trade-offs")
    assert r.backend == "cli"


def test_route_step_turno_largo_va_cli(monkeypatch, tmp_path):
    c = _orch(monkeypatch, tmp_path)
    assert c.route_step("triage", accion="palabra " * 50).backend == "cli"


def test_route_step_clase_b_sin_tools_va_cli(monkeypatch, tmp_path):
    c = _orch(monkeypatch, tmp_path)
    assert c.LOCAL_ORCH_TOOLS is False
    assert c.route_step("investigar", accion="revisa el worker").backend == "cli"
    assert c.route_step("verificar", accion="checa el done").backend == "cli"
    assert c.route_step("swarm", accion="mapea el subsistema").backend == "cli"


def test_route_step_clase_b_con_shell_va_cli(monkeypatch, tmp_path):
    c = _orch(monkeypatch, tmp_path, env={"AGENT_LOCAL_ORCH_TOOLS": "1"})
    assert c.LOCAL_ORCH_TOOLS is True
    assert c.route_step("investigar", accion="corre pytest y valida").backend == "cli"


def test_route_step_clase_b_con_tools_lectura_va_local(monkeypatch, tmp_path):
    c = _orch(monkeypatch, tmp_path, env={"AGENT_LOCAL_ORCH_TOOLS": "1"})
    assert c.route_step("investigar", accion="lee el modulo config").backend == "local"


def test_route_step_evaluar_es_clase_b(monkeypatch, tmp_path):
    # E2 (deuda #7): 'evaluar' explora el repo para comprobar el criterio -> sin tool-loop local
    # cae a cli con el modelo del tiering; con AGENT_LOCAL_ORCH_TOOLS=1 puede ir local
    c = _orch(monkeypatch, tmp_path)
    r = c.route_step("evaluar", accion="documenta worker/", done="hints al 100%")
    assert r.backend == "cli" and r.model == c.model_for("evaluar")
    c = _orch(monkeypatch, tmp_path, env={"AGENT_LOCAL_ORCH_TOOLS": "1"})
    assert c.route_step("evaluar", accion="documenta worker/", done="hints al 100%").backend == "local"


def test_route_step_veto_de_voz(monkeypatch, tmp_path):
    c = _orch(monkeypatch, tmp_path)
    c.set_voice_session(True)
    try:
        # un paso trivial que SIN voz iría local, con sesión de voz -> cli (veto de GPU)
        assert c.route_step("reflexionar", accion="lista pasos").backend == "cli"
    finally:
        c.set_voice_session(False)
    assert c.route_step("reflexionar", accion="lista pasos").backend == "local"


def test_route_step_veto_de_voz_desactivable(monkeypatch, tmp_path):
    # con LOCAL_ORCH_DISABLE_ON_VOICE=False, la voz NO veta el local
    c = _orch(monkeypatch, tmp_path, env={"AGENT_LOCAL_ORCH_DISABLE_ON_VOICE": "0"})
    c.set_voice_session(True)
    try:
        assert c.route_step("reflexionar", accion="lista pasos").backend == "local"
    finally:
        c.set_voice_session(False)


# --- Checkpoint de aprobación del plan (M1): env > [plan] toml > default ---


def test_plan_approval_default_encendida(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_PLAN_APPROVAL", raising=False)
    assert _config_fresca(monkeypatch, tmp_path).PLAN_APPROVAL is True


def test_plan_approval_del_archivo(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_PLAN_APPROVAL", raising=False)
    c = _config_fresca(monkeypatch, tmp_path, toml_text="[plan]\naprobacion = false\n")
    assert c.PLAN_APPROVAL is False


def test_plan_approval_env_gana_sobre_el_archivo(monkeypatch, tmp_path):
    c = _config_fresca(
        monkeypatch,
        tmp_path,
        toml_text="[plan]\naprobacion = false\n",
        env={"AGENT_PLAN_APPROVAL": "1"},
    )
    assert c.PLAN_APPROVAL is True


# --- Evaluación global al cerrar (E2, deuda #7): mismo esquema env > [plan] toml > default ---


def test_plan_eval_default_encendida(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_PLAN_EVAL", raising=False)
    assert _config_fresca(monkeypatch, tmp_path).PLAN_EVAL is True


def test_plan_eval_apagable_por_archivo_y_env(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_PLAN_EVAL", raising=False)
    c = _config_fresca(monkeypatch, tmp_path, toml_text="[plan]\nevaluacion = false\n")
    assert c.PLAN_EVAL is False
    c = _config_fresca(
        monkeypatch,
        tmp_path,
        toml_text="[plan]\nevaluacion = false\n",
        env={"AGENT_PLAN_EVAL": "1"},
    )
    assert c.PLAN_EVAL is True  # env gana sobre el archivo


# --- Replanificación automática (E2, pieza 2): tope de ciclos, 0 = apagada ---


def test_plan_replan_max_default(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_PLAN_REPLAN_MAX", raising=False)
    assert _config_fresca(monkeypatch, tmp_path).PLAN_REPLAN_MAX == 2


def test_plan_replan_max_cero_del_archivo_apaga(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_PLAN_REPLAN_MAX", raising=False)
    c = _config_fresca(monkeypatch, tmp_path, toml_text="[plan]\nreplan_max = 0\n")
    assert c.PLAN_REPLAN_MAX == 0  # 0 es significativo: apaga la replanificación


def test_plan_replan_max_env_gana_sobre_el_archivo(monkeypatch, tmp_path):
    c = _config_fresca(
        monkeypatch,
        tmp_path,
        toml_text="[plan]\nreplan_max = 0\n",
        env={"AGENT_PLAN_REPLAN_MAX": "5"},
    )
    assert c.PLAN_REPLAN_MAX == 5


def test_plan_replan_max_basura_y_negativo_usan_default(monkeypatch, tmp_path):
    c = _config_fresca(monkeypatch, tmp_path, env={"AGENT_PLAN_REPLAN_MAX": "muchos"})
    assert c.PLAN_REPLAN_MAX == 2
    c = _config_fresca(monkeypatch, tmp_path, env={"AGENT_PLAN_REPLAN_MAX": "-1"})
    assert c.PLAN_REPLAN_MAX == 2


# --- Identidad del usuario: el codigo publico no nombra a nadie ---
def test_usuario_sin_config_es_generico(monkeypatch, tmp_path):
    c = _config_fresca(monkeypatch, tmp_path)
    assert c.USER_NAME == "tu usuario"  # etiqueta generica, gramatical en los prompts
    assert c.USER_PROFILE == ""  # sin perfil no queda un parentesis huerfano


def test_usuario_del_archivo(monkeypatch, tmp_path):
    c = _config_fresca(
        monkeypatch,
        tmp_path,
        toml_text='[usuario]\nnombre = "Ada Lovelace"\nperfil = "analytical engines"\n',
    )
    assert c.USER_NAME == "Ada Lovelace"
    assert c.USER_PROFILE == " (analytical engines)"  # ya formateado para pegarse tras el nombre


def test_usuario_env_gana_sobre_el_archivo(monkeypatch, tmp_path):
    c = _config_fresca(
        monkeypatch,
        tmp_path,
        toml_text='[usuario]\nnombre = "Ada Lovelace"\n',
        env={"AGENT_USER_NAME": "Grace Hopper"},
    )
    assert c.USER_NAME == "Grace Hopper"


def test_usuario_vacio_o_espacios_cae_al_generico(monkeypatch, tmp_path):
    # string vacio != ausente para el toml, pero para un nombre significan lo mismo
    c = _config_fresca(monkeypatch, tmp_path, toml_text='[usuario]\nnombre = "   "\n')
    assert c.USER_NAME == "tu usuario"


def test_system_prompt_renderiza_con_la_identidad(monkeypatch, tmp_path):
    # el .format() de session.py no puede quedarse sin una clave que el markdown use
    from pathlib import Path

    c = _config_fresca(monkeypatch, tmp_path, toml_text='[usuario]\nnombre = "Ada"\nperfil = "x"\n')
    md = (Path(c.__file__).parent / "prompts" / "system.md").read_text(encoding="utf-8")
    render = md.format(AGENT_NAME=c.AGENT_NAME, USER_NAME=c.USER_NAME, USER_PROFILE=c.USER_PROFILE)
    assert "el asistente personal de Ada (x)." in render
    assert "{" not in render  # ningun placeholder sin sustituir
