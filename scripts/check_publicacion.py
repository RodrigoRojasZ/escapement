#!/usr/bin/env python
"""Guard de publicación: nada confidencial entra a este repositorio.

Este repo es **público**. El guard escanea lo que está a punto de entrar —el índice, el rango
de un push, o el árbol completo— y aborta si encuentra secretos literales, correos personales,
rutas de la máquina de desarrollo, archivos que nunca deberían versionarse, o términos vetados.

Principio de diseño: **el guard no contiene lo que protege**. Los términos concretos (nombres de
repos privados, clientes, proyectos) viven en ``.publicacion-veto.txt``, que está gitignorado;
acá solo se versiona el mecanismo y los patrones genéricos. Ver ``.publicacion-veto.example.txt``.

Uso::

    python scripts/check_publicacion.py --staged            # pre-commit: revisa el índice
    python scripts/check_publicacion.py --rango <base> <head>  # pre-push: revisa los commits
    python scripts/check_publicacion.py --arbol             # CI: revisa todo lo rastreado

Sale con 0 si está limpio y con 1 si hay hallazgos, listándolos con archivo:línea y el valor
redactado. Es conservador a propósito: un falso positivo cuesta una línea en el veto o en la
allowlist; un falso negativo publica un dato que ya no se puede despublicar.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "src"))

from agent.security.secrets import find_secrets  # noqa: E402  (tras ajustar sys.path)

VETO = RAIZ / ".publicacion-veto.txt"

# --- Patrones genéricos -------------------------------------------------------------------

# Ruta absoluta a un directorio de usuario: C:\Users\<quien>, /home/<quien>, /c/Users/<quien>.
# El grupo 1 es el componente de usuario, que es lo que se contrasta con el placeholder.
_RUTA_USUARIO = re.compile(
    r"(?:[A-Za-z]:[\\/]Users[\\/]|/[a-z]/Users/|/home/|/Users/)(?![Uu]sers?\b)([\w.-]+)",
    re.IGNORECASE,
)
# El componente de usuario cuando es obviamente un hueco a llenar, no una cuenta real. Se
# compara con fullmatch: un `search` daría por placeholder a cualquier nombre que TERMINE así.
_USUARIO_PLACEHOLDER = re.compile(
    r"tu[_-]?usuario|usuario|user(?:name)?|your[_-]?user(?:name)?|ejemplo|example|foo|bar"
    r"|\w{1,2}",  # nombres de 1-2 letras: fixtures, no cuentas reales
    re.IGNORECASE,
)
# Correo real. Se permiten los de ejemplo, los noreply y los de dominios de herramientas.
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_EMAIL_OK = re.compile(
    r"(?:@(?:example|test|localhost|invalid)\.|noreply@|@users\.noreply\.github\.com"
    r"|@anthropic\.com|@python\.org|tu[_-]?correo|your[_-]?email"
    r"|^\w{1,2}@|@\w{1,2}\.\w+$)",  # fixtures tipo t@t.co: ni la cuenta ni el dominio son reales
    re.IGNORECASE,
)
# Archivos que nunca se publican, por nombre (el .gitignore ya los cubre; esto es el cinturón).
_ARCHIVO_VETADO = re.compile(
    r"(?:^|/)(?:\.env(?!\.example$)(?:\.[\w-]+)?|escapement\.toml|id_[dr]sa|.*\.pem|.*\.key"
    r"|.*\.pfx|.*\.sqlite3?|.*\.db)$|(?:^|/)(?:data|memory-vault|\.worktrees)/",
    re.IGNORECASE,
)

# Archivos donde un patrón genérico es legítimo, con el motivo a la vista. Eximen de los
# patrones genéricos, NUNCA del veto de términos privados: eso se revisa en todos los archivos.
_ALLOWLIST: dict[str, str] = {
    "scripts/check_publicacion.py": "este guard: contiene los patrones que busca",
    "src/agent/security/secrets.py": "el detector de secretos: sus patrones de ejemplo",
    ".publicacion-veto.example.txt": "la plantilla del veto",
    "src/agent/local_models.py": 'api_key="ollama": literal que exige el cliente OpenAI local',
    "src/agent/router.py": 'api_key="ollama": idem',
    "tests/test_secrets.py": "fixtures sintéticos de secretos",
    "tests/test_check_publicacion.py": "fixtures sintéticos de este guard",
    "tests/test_config.py": "rutas de fixture",
    "tests/test_executors.py": "fixtures sintéticos de secretos",
    "tests/test_integration.py": "fixtures sintéticos de secretos",
    "tests/test_local_models.py": "fixtures del cliente local",
    "tests/test_traza.py": "identidad de git de fixture",
}


def _redactar(valor: str) -> str:
    v = valor.strip()
    return f"{v[:3]}…{v[-2:]}" if len(v) > 8 else "***"


def cargar_veto(ruta: Path = VETO) -> list[str]:
    """Lee los términos vetados locales (uno por línea; ``#`` comenta; vacío si no existe).

    Args:
        ruta: archivo de veto. Por defecto ``.publicacion-veto.txt`` en la raíz del repo.
            No está versionado a propósito: publicarlo publicaría justo lo que oculta.
    """
    if not ruta.exists():
        return []
    terminos = []
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        t = linea.split("#", 1)[0].strip()
        if t:
            terminos.append(t)
    return terminos


def revisar_texto(nombre: str, texto: str, veto: list[str]) -> list[str]:
    """Devuelve los hallazgos de un archivo como ``nombre:linea  tipo  valor redactado``.

    Args:
        nombre: ruta del archivo dentro del repo, usada para la allowlist y el reporte.
        texto: contenido a escanear. Un binario ilegible se reporta como vacío (sin hallazgos).
        veto: términos privados locales; se buscan como substring sin distinguir mayúsculas.
    """
    eximido = nombre in _ALLOWLIST
    hallazgos: list[str] = []
    for n, linea in enumerate(texto.splitlines(), 1):
        bajo = linea.lower()
        for termino in veto:  # el veto se aplica SIEMPRE, incluso a los archivos eximidos
            if termino.lower() in bajo:
                hallazgos.append(f"{nombre}:{n}  termino-vetado  {_redactar(termino)}")
        if eximido:
            continue
        for m in _RUTA_USUARIO.finditer(linea):
            if not _USUARIO_PLACEHOLDER.fullmatch(m.group(1)):
                hallazgos.append(f"{nombre}:{n}  ruta-de-maquina  {m.group(0)}")
        for m in _EMAIL.finditer(linea):
            if not _EMAIL_OK.search(m.group(0)):
                hallazgos.append(f"{nombre}:{n}  correo  {_redactar(m.group(0))}")
    if not eximido:
        hallazgos.extend(f"{nombre}  secreto  {h}" for h in find_secrets(texto))
    return hallazgos


def revisar_nombre(nombre: str) -> list[str]:
    """Devuelve un hallazgo si el archivo no debe versionarse nunca, por su nombre/ruta."""
    if nombre in _ALLOWLIST:
        return []
    return [f"{nombre}  archivo-vetado  no se publica"] if _ARCHIVO_VETADO.search(nombre) else []


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=RAIZ, capture_output=True, text=True, encoding="utf-8", errors="replace"
    ).stdout


def _contenido(nombre: str, rev: str | None) -> str:
    """Contenido del archivo en una revisión (``None`` = índice; ``""`` = árbol de trabajo)."""
    if rev == "":
        try:
            return (RAIZ / nombre).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
    spec = f"{rev}:{nombre}" if rev else f":{nombre}"
    return _git("show", spec)


def _archivos(modo: str, base: str, head: str) -> tuple[list[str], str | None]:
    """Lista de archivos a revisar y la revisión de la que leerlos."""
    if modo == "staged":
        salida = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
        return [l for l in salida.splitlines() if l], None
    if modo == "arbol":
        return [l for l in _git("ls-files").splitlines() if l], ""
    if base.strip("0") == "":  # rama nueva: no hay base, se revisa todo el árbol del head
        salida = _git("ls-tree", "-r", "--name-only", head)
    else:
        salida = _git("diff", "--name-only", "--diff-filter=ACMR", base, head)
    return [l for l in salida.splitlines() if l], head


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Guard de publicación del repo público.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--staged", action="store_true", help="revisa el índice (pre-commit)")
    g.add_argument("--arbol", action="store_true", help="revisa todo lo rastreado (CI)")
    g.add_argument("--rango", nargs=2, metavar=("BASE", "HEAD"), help="revisa un push")
    args = p.parse_args(argv)

    # El hook corre bajo la consola de Windows, que por defecto no es UTF-8: sin esto los
    # acentos del reporte salen como basura justo cuando hay que leerlo con atención.
    for flujo in (sys.stderr, sys.stdout):
        if hasattr(flujo, "reconfigure"):
            flujo.reconfigure(encoding="utf-8", errors="replace")

    modo = "staged" if args.staged else "arbol" if args.arbol else "rango"
    base, head = args.rango if args.rango else ("", "")
    archivos, rev = _archivos(modo, base, head)

    veto = cargar_veto()
    hallazgos: list[str] = []
    for nombre in archivos:
        hallazgos.extend(revisar_nombre(nombre))
        hallazgos.extend(revisar_texto(nombre, _contenido(nombre, rev), veto))

    if hallazgos:
        sys.stderr.write("\n[guard de publicación] BLOQUEADO: este repo es público.\n\n")
        for h in hallazgos:
            sys.stderr.write(f"  {h}\n")
        sys.stderr.write(
            "\nQuita el dato, o —si es deliberado— agrégalo a _ALLOWLIST en "
            "scripts/check_publicacion.py.\nSaltarse el guard (--no-verify) publica el dato "
            "para siempre: no se puede despublicar.\n\n"
        )
        return 1

    if not veto:
        sys.stderr.write(
            "[guard de publicación] aviso: no hay .publicacion-veto.txt; "
            "solo corrieron los patrones genéricos.\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
