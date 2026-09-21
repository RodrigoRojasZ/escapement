"""Tests del mini-parser de argumentos (agent.cliparse). Puro: sin I/O, sin Chrome/API."""

from agent import cliparse


# --- split_args: estructura -------------------------------------------------
def test_solo_posicionales():
    assert cliparse.split_args(["a", "b"]) == (["a", "b"], {})


def test_lista_vacia():
    assert cliparse.split_args([]) == ([], {})


def test_flag_con_valor_separado():
    assert cliparse.split_args(["--limit", "5"]) == ([], {"limit": "5"})


def test_flag_con_valor_inline():
    assert cliparse.split_args(["--limit=5"]) == ([], {"limit": "5"})


def test_inline_vacio_es_string_vacio():
    # --clave= es un valor explícito vacío, no una bandera
    assert cliparse.split_args(["--repo="]) == ([], {"repo": ""})


def test_flag_booleana_declarada_no_consume():
    pos, opts = cliparse.split_args(["--revisar", "algo"], flags_bool=frozenset({"revisar"}))
    assert opts == {"revisar": True}
    assert pos == ["algo"]


def test_flag_sin_valor_al_final_es_true():
    assert cliparse.split_args(["--verbose"]) == ([], {"verbose": True})


def test_flag_no_consume_a_otra_flag():
    # --a no debe tragarse --b como su valor
    assert cliparse.split_args(["--a", "--b"]) == ([], {"a": True, "b": True})


def test_corto_mapea_a_largo():
    assert cliparse.split_args(["-n", "5"], short_map={"n": "limit"}) == ([], {"limit": "5"})


def test_escape_deja_el_resto_literal():
    pos, opts = cliparse.split_args(["--", "--urgente", "arregla"])
    assert pos == ["--urgente", "arregla"]
    assert opts == {}


def test_guion_solo_es_posicional():
    assert cliparse.split_args(["-"]) == (["-"], {})


def test_posicionales_y_opciones_mezclados():
    pos, opts = cliparse.split_args(["repo", "--limit", "30"])
    assert pos == ["repo"]
    assert opts == {"limit": "30"}


# --- split_args: stop_at_positional ----------------------------------------
def test_stop_at_positional_congela_la_frase():
    # tras el primer posicional, incluso los --x quedan literales (frase libre preservada)
    pos, opts = cliparse.split_args(
        ["--repo", "X", "arregla", "el", "--bug"], stop_at_positional=True
    )
    assert opts == {"repo": "X"}
    assert pos == ["arregla", "el", "--bug"]


def test_stop_at_positional_sin_flags_frontales():
    pos, opts = cliparse.split_args(["arregla", "--force", "ya"], stop_at_positional=True)
    assert opts == {}
    assert pos == ["arregla", "--force", "ya"]


# --- arg_int ----------------------------------------------------------------
def test_arg_int_digito():
    assert cliparse.arg_int(["5"], 0, default=0) == 5


def test_arg_int_ausente_usa_default():
    assert cliparse.arg_int([], 0, default=20) == 20


def test_arg_int_no_numero_no_crashea():
    assert cliparse.arg_int(["abc"], 0, default=20) == 20  # antes: ValueError


def test_arg_int_negativo():
    assert cliparse.arg_int(["-5"], 0, default=0) == -5


def test_arg_int_segundo_posicional():
    assert cliparse.arg_int(["repo", "30"], 1, default=20) == 30


def test_arg_int_default_none():
    assert cliparse.arg_int(["x"], 0, default=None) is None


# --- opt_int ----------------------------------------------------------------
def test_opt_int_prioriza_primera_clave_presente():
    assert cliparse.opt_int({"limit": "7"}, "limit", "n", default=0) == 7
    assert cliparse.opt_int({"n": "7"}, "limit", "n", default=0) == 7


def test_opt_int_ausente_usa_default():
    assert cliparse.opt_int({}, "limit", "n", default=15) == 15


def test_opt_int_valor_no_numerico_usa_default():
    assert cliparse.opt_int({"limit": "abc"}, "limit", default=15) == 15


def test_opt_int_booleana_no_cuenta():
    assert cliparse.opt_int({"limit": True}, "limit", default=15) == 15


# --- opt_str / has_flag -----------------------------------------------------
def test_opt_str_presente():
    assert cliparse.opt_str({"repo": "mi_repo"}, "repo") == "mi_repo"


def test_opt_str_booleana_devuelve_default():
    assert cliparse.opt_str({"repo": True}, "repo", default="") == ""


def test_opt_str_ausente_default():
    assert cliparse.opt_str({}, "repo", default="x") == "x"


def test_has_flag():
    assert cliparse.has_flag({"revisar": True}, "revisar") is True
    assert cliparse.has_flag({"repo": "x"}, "revisar") is False
