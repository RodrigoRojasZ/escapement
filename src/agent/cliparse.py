"""Parseo uniforme de argumentos de subcomando del CLI.

Separa los tokens de un subcomando en posicionales y opciones (``--clave valor`` /
``--clave=valor`` / ``--flag`` / ``-x`` corto) y los coerciona con defaults SIN reventar ante input
inválido. Es la pieza que reemplaza el parseo posicional disperso de ``agent.cli`` (que hoy es
inconsistente entre comandos y, en varios, crashea con ``ValueError`` ante un no-número).

Puro: no toca disco, red ni estado global. Cada handler ``_run_*`` de ``agent.cli`` lo llama al
inicio para dejar de reimplementar el parseo a mano. No conoce el dominio (repos, planes): solo
separa estructura; el handler decide qué significa cada posicional/opción.
"""

from __future__ import annotations

_ESCAPE = "--"  # un token `--` suelto: todo lo que le sigue es posicional literal (escape)


def split_args(
    argv: list[str],
    *,
    flags_bool: frozenset[str] = frozenset(),
    short_map: dict[str, str] | None = None,
    stop_at_positional: bool = False,
) -> tuple[list[str], dict[str, str | bool]]:
    """Separa ``argv`` en ``(posicionales, opciones)``. No valida el dominio: solo estructura.

    Reglas de tokens:
        - ``--clave valor`` -> ``opciones["clave"] = "valor"`` (consume el siguiente token) SALVO que
          ``clave`` esté en ``flags_bool`` o no haya un valor consumible (siguiente token ausente o
          que empiece con ``-``): en ese caso ``opciones["clave"] = True``.
        - ``--clave=valor`` -> ``opciones["clave"] = "valor"`` (nunca consume el siguiente token; el
          valor puede quedar vacío con ``--clave=``).
        - ``-x`` corto -> se traduce con ``short_map`` (p.ej. ``{"n": "limit"}``) y se procesa igual
          que su forma larga; un corto sin mapeo conserva su propia letra como clave.
        - ``--`` suelto (escape): agota el resto de ``argv`` como posicionales literales.
        - cualquier otro token (incluido un ``-`` solo): posicional.

    Un valor que deba empezar con ``-`` (raro en este CLI) se pasa con ``--clave=valor``, porque un
    token suelto que empieza con ``-`` siempre se interpreta como opción, nunca como valor.

    Args:
        argv: tokens DESPUÉS del nombre del subcomando (lo que hoy recibe cada ``_run_*``).
        flags_bool: claves que son banderas booleanas (no consumen valor), p.ej. ``frozenset({"revisar"})``.
            El default ``frozenset()`` (vacío) significa "ninguna clave es booleana declarada": una
            ``--clave`` sin valor consumible igual degrada a ``True``, no crashea.
        short_map: alias de un carácter -> nombre largo, p.ej. ``{"n": "limit"}``. ``None`` = sin
            atajos cortos (equivale a ``{}``).
        stop_at_positional: si True, en cuanto aparece el PRIMER posicional TODO lo que sigue
            (incluidos los ``--x``) se trata como posicional. Es el modo "flags al frente, luego frase
            libre" para comandos cuyo resto es texto (objetivo->meta, paso->nota): preserva EXACTO el
            ``" ".join(...)`` de hoy y aun así admite ``--repo`` adelante. Con False, las opciones se
            reconocen en cualquier posición.

    Returns:
        ``(posicionales, opciones)``. ``opciones`` mapea nombre-largo -> ``str`` (con valor) o
        ``True`` (booleana). Nunca lanza: un input raro degrada a posicional o a bandera ``True``.
    """
    short_map = short_map or {}
    pos: list[str] = []
    opts: dict[str, str | bool] = {}
    i, n = 0, len(argv)
    while i < n:
        tok = argv[i]
        if tok == _ESCAPE:  # escape: el resto es literal
            pos.extend(argv[i + 1 :])
            break
        es_opcion = tok.startswith("-") and tok != "-"
        if es_opcion and not (stop_at_positional and pos):
            clave, sep, inline = tok.lstrip("-").partition("=")
            if len(tok) >= 2 and tok[1] != "-":  # corto -x -> nombre largo
                clave = short_map.get(clave, clave)
            if sep:  # forma --clave=valor (valor explícito, aunque sea vacío)
                opts[clave] = inline
            elif clave in flags_bool:
                opts[clave] = True
            else:
                nxt = argv[i + 1] if i + 1 < n else None
                if nxt is None or nxt.startswith("-"):
                    opts[clave] = True  # sin valor consumible: bandera booleana
                else:
                    opts[clave] = nxt
                    i += 1  # consume el valor
        else:
            pos.append(tok)
        i += 1
    return pos, opts


def arg_int(pos: list[str], index: int, *, default: int | None) -> int | None:
    """Entero del posicional ``index``, o ``default`` si falta o no es un entero. NO crashea.

    Reemplaza los ``int(argv[i])`` sin guardia del CLI: ante un no-número devuelve ``default`` en
    vez de lanzar ``ValueError``.

    Args:
        pos: lista de posicionales (de :func:`split_args`).
        index: posición a leer (0-based); fuera de rango -> ``default``.
        default: valor si el posicional falta o no coerciona; ``None`` es válido (p.ej. id requerido).
    """
    if 0 <= index < len(pos):
        tok = pos[index]
        cuerpo = tok[1:] if tok[:1] in "+-" else tok
        if cuerpo.isdigit():
            return int(tok)
    return default


def opt_int(opts: dict[str, str | bool], *keys: str, default: int | None) -> int | None:
    """Primer entero presente entre ``keys`` (p.ej. ``"limit"``, ``"n"``), o ``default``. NO crashea.

    Args:
        opts: opciones (de :func:`split_args`).
        keys: claves a probar en orden; gana la primera con un valor entero válido.
        default: valor si ninguna clave tiene un entero (ausente, booleana o texto no numérico).
    """
    for k in keys:
        v = opts.get(k)
        if isinstance(v, str):
            cuerpo = v[1:] if v[:1] in "+-" else v
            if cuerpo.isdigit():
                return int(v)
    return default


def opt_str(opts: dict[str, str | bool], *keys: str, default: str = "") -> str:
    """Primer valor de texto no vacío entre ``keys``, o ``default``.

    Una bandera booleana (``True``) NO cuenta como texto (devuelve ``default``): así ``--repo`` sin
    valor no se confunde con un repo llamado ``"True"``.
    """
    for k in keys:
        v = opts.get(k)
        if isinstance(v, str) and v:
            return v
    return default


def has_flag(opts: dict[str, str | bool], *keys: str) -> bool:
    """True si alguna de ``keys`` está presente (como bandera ``True`` o con cualquier valor)."""
    return any(k in opts for k in keys)
