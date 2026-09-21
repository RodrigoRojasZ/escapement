"""Tests del guard de publicación (scripts/check_publicacion.py). Sin git, sin red, sin cuota.

Se prueban las tres funciones puras —``revisar_texto``, ``revisar_nombre`` y ``cargar_veto``—,
que son las que deciden si algo se publica. Lo que consulta git (``_archivos``, ``_contenido``)
no se toca acá: es una envoltura fina sobre ``git diff``/``git show``.
"""

import importlib.util
from pathlib import Path

_RUTA = Path(__file__).resolve().parent.parent / "scripts" / "check_publicacion.py"
_spec = importlib.util.spec_from_file_location("check_publicacion", _RUTA)
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)


# --- rutas de la máquina de desarrollo -----------------------------------------------------


def test_detecta_ruta_windows_de_usuario():
    assert cp.revisar_texto("docs/x.md", r"vive en C:\Users\alguien\proyecto", [])


def test_detecta_ruta_posix_de_usuario():
    assert cp.revisar_texto("docs/x.md", "cd /home/alguien/proyecto", [])
    assert cp.revisar_texto("docs/x.md", "cd /c/Users/alguien/proyecto", [])


def test_placeholder_de_usuario_no_es_hallazgo():
    plantilla = "ruta = 'C:/Users/tu-usuario/escapement'  # y /home/x para el fixture"
    assert cp.revisar_texto("escapement.toml.example", plantilla, []) == []


def test_el_placeholder_se_compara_entero_no_por_sufijo():
    # 'alguien' termina en 'en' (1-2 letras); con `search` se colaba como placeholder
    assert cp.revisar_texto("docs/x.md", r"C:\Users\alguien\x", [])


def test_ruta_relativa_no_es_hallazgo():
    assert cp.revisar_texto("docs/x.md", "ver src/agent/config.py y ./scripts/x.sh", []) == []


# --- correos ------------------------------------------------------------------------------


def test_detecta_correo_personal():
    assert cp.revisar_texto("README.md", "escribe a alguien.real@gmail.com", [])


def test_correo_noreply_o_de_ejemplo_no_es_hallazgo():
    limpio = (
        "Co-Authored-By: Claude <noreply@anthropic.com>\n"
        "autor: 144026559+usuario@users.noreply.github.com\n"
        "ejemplo: tu_correo@example.com\n"
    )
    assert cp.revisar_texto("README.md", limpio, []) == []


# --- términos vetados ---------------------------------------------------------------------


def test_detecta_termino_vetado_sin_distinguir_mayusculas():
    assert cp.revisar_texto("docs/x.md", "migrar desde Repo_Privado hoy", ["repo_privado"])


def test_sin_veto_no_hay_hallazgo_de_termino():
    assert cp.revisar_texto("docs/x.md", "migrar desde Repo_Privado hoy", []) == []


def test_veto_vacio_no_es_lo_mismo_que_none(tmp_path):
    assert cp.cargar_veto(tmp_path / "no-existe.txt") == []
    archivo = tmp_path / "veto.txt"
    archivo.write_text("# comentario\n\nuno\ndos  # al final\n", encoding="utf-8")
    assert cp.cargar_veto(archivo) == ["uno", "dos"]


# --- allowlist ----------------------------------------------------------------------------


def test_allowlist_exime_al_propio_guard_y_al_detector():
    sucio = r"C:\Users\rodolfo\x  y persona.real@gmail.com"
    assert cp.revisar_texto("scripts/check_publicacion.py", sucio, []) == []
    assert cp.revisar_texto("src/agent/security/secrets.py", sucio, []) == []
    # un archivo cualquiera con el mismo contenido SÍ se reporta
    assert cp.revisar_texto("docs/otro.md", sucio, [])


def test_el_veto_se_aplica_incluso_a_los_archivos_eximidos():
    # la allowlist perdona patrones genéricos, nunca un término privado
    assert cp.revisar_texto("scripts/check_publicacion.py", "usa repo_privado", ["repo_privado"])


# --- archivos vetados por nombre -----------------------------------------------------------


def test_archivos_que_nunca_se_publican():
    for nombre in (".env", ".env.local", "escapement.toml", "data/ledger.json",
                   "certs/server.pem", "memory-vault/nota.md", "local/app.sqlite"):
        assert cp.revisar_nombre(nombre), nombre


def test_plantillas_y_codigo_normal_si_se_publican():
    for nombre in (".env.example", "src/agent/config.py", "docs/VISION.md", "README.md"):
        assert cp.revisar_nombre(nombre) == [], nombre


# --- secretos literales (delegado a agent.security.secrets) --------------------------------


def test_delega_la_deteccion_de_secretos():
    assert cp.revisar_texto("src/x.py", 'api_key = "sk-abcdefghijklmnopqrstuvwxyz012345"', [])


def test_texto_limpio_no_reporta_nada():
    limpio = 'KEY = os.getenv("ESCAPEMENT_API_KEY")\n# ver docs/VISION.md\n'
    assert cp.revisar_texto("src/x.py", limpio, []) == []
