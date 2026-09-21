# Diseño: mini-parser uniforme de argumentos del CLI (S1)

**Fecha:** 2026-07-18 · **Estado:** entregado (`cliparse`)
**Origen:** hallazgo **B** de la retro de DX del 2026-07-18 (`docs/RETRO_DX_PLANES.md`, retirado el
2026-07-31 y reemplazado por [`DEUDAS.md`](DEUDAS.md); sigue en el historial de git) — manejo de argumentos posicional,
frágil y sin ayuda). Cierra **B1** (crashes por `int()` sin guard), **B2** (heurística posicional de repo)
y **B3** (sin ayuda por subcomando) con una sola pieza reutilizable.

---

## 1 · Objetivo y no-objetivos

**Objetivo:** un separador `posicionales / opciones` mínimo y **puro**, más helpers de coerción tipada,
que cada handler llama al inicio. Unifica el parseo, elimina los crashes, habilita flags (`--repo`,
`--limit`) y ayuda por subcomando — **sin cambiar el comportamiento observable** salvo dos casos marcados
(§7).

**No-objetivos (intactos):**
- El dispatch por tabla `_COMMANDS` + `_ALIASES` + sugerencia de typos con `difflib`
  ([`cli.py:597-616`](../src/agent/cli.py#L597)).
- La firma de los handlers `_run_*(argv: list[str]) -> None` — el parser vive **dentro** de cada handler,
  no la reemplaza. Por eso `tests/test_cli.py` sigue verde sin tocarlo.
- `main()` y el entry point `python -m agent.cli --voz` que usa [`tray.py:51`](../src/agent/voice/tray.py#L51).
- Los nombres y alias actuales de comandos.

---

## 2 · Decisión: mini-parser custom, **no** `argparse`

| Criterio | `argparse` | Mini-parser custom |
|---|---|---|
| Ayuda `--help` autogenerada | ✅ gratis | ⚠️ manual (barato: los `uso:` ya existen) |
| Validación de tipos | ✅ | ⚠️ helpers propios (~15 líneas) |
| Encaja "el resto es una frase libre" (meta/directiva/nota) | ❌ `nargs=REMAINDER`/`'+'` con gotchas | ✅ `stop_at_positional` explícito |
| Expresa la **heurística posicional de repo** (B2) | ❌ posicionales de orden fijo → hay que pre-procesar igual | ✅ el handler decide con el dict |
| Conserva tabla + alias + `difflib` de typos | ❌ subparsers reimplementan todo eso | ✅ no los toca |
| No llama `sys.exit()` en error/`--help` | ❌ (mitigable con `exit_on_error=False`) | ✅ imprime y retorna, como hoy |
| Acoplamiento con `main`/`-m agent.cli --voz` | ❌ quiere ser dueño del top-level | ✅ cero |
| Líneas de código nuevas | 15 subparsers ≈ 150+ | ~70 (un módulo testeable) |

`argparse` brilla cuando **empiezas de cero** y aceptas su modelo de posicionales fijos. Aquí ya hay un
dispatch propio con heurística de repo, frase libre, alias y typo-suggest; encajar `argparse` cuesta
**más** código y pierde piezas. El mini-parser es más chico y respeta todo lo que ya funciona.

---

## 3 · Contrato de integración

```
main() → _dispatch(argv)            # SIN CAMBIO (tabla, alias, difflib, --help global)
           └─ _COMMANDS[cmd](args)  # SIN CAMBIO de firma: _run_*(argv: list[str])
                └─ pos, opts = cliparse.split_args(argv, ...)   # ← el parser entra AQUÍ
                   limit = cliparse.opt_int(opts, "limit", "n", default=...)
```

El parser es una **utilidad interna** que cada handler invoca. Ningún test existente lo nota:
`_run_plan_cmd(["activar", repo])`, `_dispatch(["log"])`, etc. siguen recibiendo `list[str]`.

---

## 4 · API del módulo nuevo `agent/cliparse.py`

Módulo puro (sin I/O, sin lazy imports pesados) → testeable en aislamiento como `test_fsutil`/`test_config`.

> **Implementado** en [`src/agent/cliparse.py`](../src/agent/cliparse.py) — esa es la fuente de verdad.
> El bloque de abajo es el diseño; difiere del código final solo en detalles equivalentes (p.ej. el
> guard del valor inline se escribe con el `sep` de `partition("=")`, no re-chequeando `"=" in tok`).

```python
"""Parseo uniforme de argumentos de subcomando del CLI: separa posicionales de opciones
(--clave valor / --clave=valor / --flag) y coerciona con defaults SIN reventar en input inválido.

Puro: no toca disco, red ni estado. Cada handler de agent.cli lo llama al inicio para dejar de
reimplementar el parseo posicional (que hoy es inconsistente y, en varios comandos, crashea).
"""

from __future__ import annotations

_ESCAPE = "--"  # todo lo que sigue a un token `--` suelto es posicional literal


def split_args(
    argv: list[str],
    *,
    flags_bool: frozenset[str] = frozenset(),
    short_map: dict[str, str] | None = None,
    stop_at_positional: bool = False,
) -> tuple[list[str], dict[str, str | bool]]:
    """Separa ``argv`` en (posicionales, opciones). No valida el dominio: solo estructura.

    Reglas de tokens:
        - ``--clave valor`` -> ``opciones["clave"] = "valor"`` (consume el siguiente token) SALVO que
          ``clave`` esté en ``flags_bool`` o no haya un valor consumible (siguiente es otra opción,
          el escape ``--`` o fin de lista): entonces ``opciones["clave"] = True``.
        - ``--clave=valor`` -> ``opciones["clave"] = "valor"`` (nunca consume el siguiente token).
        - ``-x`` corto -> se traduce con ``short_map`` (p.ej. ``{"n": "limit"}``) y se procesa como
          ``--<largo>``; un corto sin mapeo se trata como opción booleana con su propia letra.
        - ``--`` suelto (escape): agota el resto como posicionales literales, sin interpretar ``-``.
        - cualquier otro token: posicional.

    Args:
        argv: tokens DESPUÉS del nombre del subcomando (lo que hoy recibe cada ``_run_*``).
        flags_bool: claves que son banderas booleanas (no consumen valor), p.ej. ``{"revisar"}``.
        short_map: alias de un carácter -> nombre largo, p.ej. ``{"n": "limit", "h": "help"}``.
        stop_at_positional: si True, en cuanto aparece el PRIMER posicional, TODO lo que sigue
            (incluidos ``--x``) se trata como posicional. Es el modo "flags al frente, luego frase
            libre" para comandos cuyo resto es texto (objetivo->meta, paso->nota): preserva EXACTO el
            ``" ".join(...)`` de hoy y aun así admite ``--repo`` adelante. Con False, las opciones se
            reconocen en cualquier posición (lo que usa `optimiza`: `--directiva` va en cualquier lado).

    Returns:
        ``(posicionales, opciones)``. ``opciones`` mapea nombre-largo -> ``str`` (con valor) o
        ``True`` (booleana). Nunca lanza: input raro degrada a posicional o bandera.
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
            clave, _, inline = tok.lstrip("-").partition("=")
            if len(tok) >= 2 and tok[1] != "-":  # corto -x
                clave = short_map.get(clave, clave)
            if inline or "=" in tok:
                opts[clave] = inline
            elif clave in flags_bool:
                opts[clave] = True
            else:
                nxt = argv[i + 1] if i + 1 < n else None
                if nxt is None or nxt == _ESCAPE or (nxt.startswith("--")):
                    opts[clave] = True
                else:
                    opts[clave] = nxt
                    i += 1
        else:  # posicional (o, con stop_at_positional, ya empezó la frase libre)
            pos.append(tok)
        i += 1
    return pos, opts


def arg_int(pos: list[str], index: int, *, default: int | None) -> int | None:
    """Entero del posicional ``index`` (default si falta o no es dígito). NO crashea (reemplaza int())."""
    if 0 <= index < len(pos):
        tok = pos[index]
        cuerpo = tok[1:] if tok[:1] in "+-" else tok
        if cuerpo.isdigit():
            return int(tok)
    return default


def opt_int(opts: dict[str, str | bool], *keys: str, default: int | None) -> int | None:
    """Primer entero presente entre ``keys`` (--limit/-n); default si falta o no coerciona. No crashea."""
    for k in keys:
        v = opts.get(k)
        if isinstance(v, str):
            cuerpo = v[1:] if v[:1] in "+-" else v
            if cuerpo.isdigit():
                return int(v)
    return default


def opt_str(opts: dict[str, str | bool], *keys: str, default: str = "") -> str:
    """Primer valor string presente entre ``keys``; default si ninguno tiene valor de texto."""
    for k in keys:
        v = opts.get(k)
        if isinstance(v, str) and v:
            return v
    return default


def has_flag(opts: dict[str, str | bool], *keys: str) -> bool:
    """True si alguna de ``keys`` está presente (como bandera True o con cualquier valor)."""
    return any(k in opts for k in keys)
```

---

## 5 · Gramática unificada por comando (antes → después)

`SAP` = `stop_at_positional`. Todo lo nuevo es **aditivo**: sin flags, el comportamiento es el de hoy.

| Comando | Hoy | Flags nuevos | SAP | Backward-compat |
|---|---|---|---|---|
| `optimiza <ruta> [directiva]` | `argv[0]`, `argv[1]` | `--directiva` | — | ruta=pos0, directiva=pos1 |
| `backlog <repo> [N]` | `argv[0]`, `int(argv[1])`💥 | `--repo`, `--limit`/`-n` | — | pos0=repo, pos1=N |
| `vigilar <repo> [N]` | `argv[0]`, `int(argv[1])`💥 | `--repo`, `--limit`/`-n` | — | idem |
| `trabajar [N]` | `int(argv[0]) if isdigit` | `--limit`/`-n` | — | pos0=N |
| `historial [N]` | `int(argv[0]) if isdigit` | `--limit`/`-n` | — | pos0=N |
| `objetivo [repo] "<meta>"` | heurística + `" ".join` | `--repo` | ✅ | heurística = fallback; meta libre EXACTA |
| `ejecutar [repo]` | `argv[0]` heurística | `--repo` | — | idem |
| `paso <id> <estado> [nota]` | `int(argv[0])`💥, `argv[1]`, join | — (nota = frase libre) | ✅ | id/estado pos; nota libre |
| `agentes [dest]` | `argv[0]` | `--dest` | — | pos0=dest |
| `plan [lista\|activar <repo>]` | sub-dispatch | (sin cambio ahora) | — | idem |
| `memoria [repos...]` | varargs | — | — | idem |
| `repaso [repos...]` | varargs | — | — | idem |
| `autoevoluciona <repo> [N]` | delega | `--repo`, `--limit`/`-n` | — | delega |

💥 = hoy crashea con input no numérico (ver §7.1).

**Consistencia ganada:** `--limit`/`-n` es el MISMO flag en los 5 comandos que toman `N` (hoy cada uno lo
parsea distinto y con `argv[0]` o `argv[1]` según el comando).

### 5.1 · Ejemplos de refactor (3 handlers representativos)

**`trabajar` — flags en cualquier posición (SAP off):**
```python
def _run_trabajar(argv: list[str]) -> None:
    from agent import cliparse
    from agent.orchestrator import run_queue
    pos, opts = cliparse.split_args(argv, short_map={"n": "limit"})
    limit = cliparse.opt_int(opts, "limit", "n", default=cliparse.arg_int(pos, 0, default=0))
    ...
```
`trabajar 5`, `trabajar --limit 5`, `trabajar -n 5` → 5. `trabajar abc` → 0 (como hoy).

**`objetivo` — `--repo` explícito + heurística como fallback (SAP on preserva la meta):**
```python
def _run_objetivo(argv: list[str]) -> None:
    from agent import cliparse, planner, reasoning
    pos, opts = cliparse.split_args(argv, stop_at_positional=True)
    if "repo" in opts:                                  # explícito: cero ambigüedad (B2)
        repo = _resolve_repo(cliparse.opt_str(opts, "repo")); meta = pos
    elif pos and _es_repo(pos[0]):                      # heurística de hoy: fallback
        repo = _resolve_repo(pos[0]); meta = pos[1:]
    else:
        repo = None; meta = pos
    objetivo = " ".join(meta)
    ...
```
`objetivo mi_repo "arregla X"` → igual que hoy. `objetivo --repo mi_repo arregla X`
→ repo explícito, meta libre. `objetivo arregla el bug` → sin repo, meta completa (igual que hoy).

**`paso` — id requerido validado (adiós al crash):**
```python
def _run_paso(argv: list[str]) -> None:
    from agent import cliparse, planner
    pos, _ = cliparse.split_args(argv, stop_at_positional=True)  # sin --nota: la nota es frase libre
    pid = cliparse.arg_int(pos, 0, default=None)
    if pid is None or len(pos) < 2:
        print('uso: escapement paso <id> <hecho|pendiente|fallido|bloqueado> ["nota"]'); return
    estado = pos[1].lower()
    nota = " ".join(pos[2:]) if len(pos) > 2 else None
    ...
```
`paso 4 hecho "resolví así"` → igual que hoy. `paso x hecho` → mensaje de uso (hoy: **traceback**).

---

## 6 · Ayuda por subcomando (B3)

`_COMMANDS` pasa de `tuple[handler, str]` a un `dataclass` liviano (las **claves** no cambian → los tests
`test_comandos_esperados_registrados` y `test_dispatch_help_lista_comandos` siguen verdes):

```python
@dataclass(frozen=True)
class Command:
    handler: Callable[[list[str]], None]
    resumen: str                     # la línea que ya existe (para `help` global)
    uso: str = ""                    # firma larga; default = resumen (para `help <cmd>`)
```

En `_dispatch`, antes de invocar el handler:
```python
if len(args) >= 2 and args[1] in ("-h", "--help", "help"):
    print(_COMMANDS[cmd].uso or _COMMANDS[cmd].resumen); return True
```
→ `escapement objetivo --help` y `escapement help objetivo` imprimen el uso de ese comando. Los strings `uso:`
que hoy viven dispersos en los handlers ([`cli.py:283`](../src/agent/cli.py#L283),
[`:370`](../src/agent/cli.py#L370), …) se consolidan en el campo `uso` — fuente única.

---

## 7 · Cambios de comportamiento observables (requieren tu OK)

Un refactor no debe alterar el comportamiento observable; estos dos lo hacen **a propósito** y por eso van
marcados y separados, no colados en el diff.

### 7.1 · Crash → mensaje limpio (mejora)
`escapement paso x hecho`, `escapement backlog repo abc`, `escapement vigilar repo abc` hoy revientan con
`ValueError`/traceback ([`cli.py:377`](../src/agent/cli.py#L377), [`:51`](../src/agent/cli.py#L51),
[`:89`](../src/agent/cli.py#L89)). Después imprimen un `uso:`/default y retornan. **Salida distinta**
(traceback → texto), aunque estrictamente mejor. Necesita tu visto bueno como cambio de comportamiento.

### 7.2 · Meta que EMPIEZA con `--` (caso extremo)
Con `stop_at_positional`, la frase libre se preserva salvo si su **primer token** es `--palabra`:
`escapement objetivo --urgente arregla` interpretaría `--urgente` como opción. Mitigación estándar: el escape
`--` → `escapement objetivo -- --urgente arregla`. Se documenta en README. Impacto real ≈ nulo (nadie
empieza una meta con `--palabra`), pero lo dejo explícito.

Todo lo demás (posicionales, `" ".join` de metas normales, defaults de `N`) es **idéntico**.

---

## 8 · Plan de tests aislados (`tests/test_cliparse.py`, estilo `test_cli.py`)

Sin API/Chrome/DB; el parser es puro. Casos:

- **split_args:** posicional simple; `--clave valor`; `--clave=valor`; `--flag` booleano (flags_bool);
  `--flag` al final sin valor → True; dos flags seguidos (`--a --b`) → `--a` no consume `--b`; corto `-n 5`
  vía short_map; escape `--` deja el resto literal; `stop_at_positional` corta en el primer posicional;
  lista vacía → `([], {})`.
- **arg_int / opt_int:** dígito válido; ausente → default; `"abc"` → default (no crashea); negativos;
  `opt_int` prioriza la primera key presente.
- **opt_str / has_flag:** presente vs default; bandera booleana no cuenta como string.
- **Integración por handler (en `test_cli.py`, con `capsys`):** `trabajar 5` == `trabajar --limit 5`;
  `paso x hecho` imprime uso y NO lanza; `objetivo --repo <k> meta` fija repo y meta; `objetivo <repo> meta`
  (heurística) sigue igual; `backlog repo abc` no crashea.
- **Regresión:** re-correr toda la suite existente (33 archivos) — debe quedar verde sin editar
  `test_cli.py`.

---

## 9 · Entregables (reglas de API pública del proyecto)

El CLI es la API pública del paquete → aplican las reglas globales:

1. **Type hints + docstrings con `Args:`** en `cliparse.py` y en las firmas nuevas (ya en §4).
2. **Tests aislados** en `tests/test_cliparse.py` + integración en `test_cli.py` (§8).
3. **README:** nueva sub-sección "Flags y ayuda por comando" en [`⚡ Acceso rápido`](../README.md#L155):
   documentar `--repo`, `--limit`/`-n`, `--help`, el escape `--`, y actualizar la tabla (que hoy **ni
   siquiera lista** `objetivo`/`ejecutar`/`plan`/`paso` — deuda de doc que aprovecho a cerrar).
4. **Bump de versión:** `0.3.0 → 0.4.0` en [`pyproject.toml:3`](../pyproject.toml#L3) (feature
   backward-compat = minor).
5. **`ruff format`** sobre cada `.py` tocado, solo líneas que cambian.

---

## 10 · Implementación incremental (orden seguro, con verificación cruzada)

| Paso | Qué | Verificación cruzada |
|---|---|---|
| 1 | Crear `agent/cliparse.py` + `tests/test_cliparse.py` | `uv run pytest -k cliparse` verde; nada más lo importa aún |
| 2 | Refactor de los handlers **sin flags nuevos** (solo `arg_int`/`opt_int` donde hoy hay `int()`) | suite completa verde; **diff de salida** de cada comando idéntico salvo §7.1 |
| 3 | Agregar `--repo` (objetivo/ejecutar/backlog/vigilar) y `--limit`/`-n` | tests de integración nuevos; heurística de repo intacta como fallback |
| 4 | `Command` dataclass + `help <cmd>`/`--help` | `test_dispatch_help_lista_comandos` verde; nuevo test de `help <cmd>` |
| 5 | README + bump `0.4.0` + `ruff format` | `uv run pytest` full verde |

Cada paso es un commit atómico y reversible; el paso 2 es donde se demuestra la **preservación de
comportamiento** (diff antes/después por comando), coherente con la regla de refactor del proyecto.

---

## 11 · Fuera de alcance (enganchan aquí después)

- **M1 checkpoint de aprobación** reusará `--revisar` en `ejecutar` (el parser ya lo soporta como
  `flags_bool={"revisar"}`).
- **M2 slash-commands en el REPL** puede reusar `split_args` para parsear `/meta --repo X …`.
- **Sub-dispatch de `plan`** (lista/activar) se deja igual ahora; si crece, migra al mismo parser.

---

## 12 · Estado de entrega (2026-07-19)

**Implementado y en verde** (`v0.4.0`). Dos desviaciones respecto al diseño de arriba, ambas hacia
**menos** superficie de API:

- **`optimiza` NO usa `stop_at_positional`** (tabla §5): quedó con `split_args` plano, así `--directiva`
  se reconoce en cualquier posición y no solo al frente. La directiva posicional (`optimiza <ruta> <dir>`)
  sigue idéntica.
- **`paso` NO expone un flag `--nota`** (tabla §5, ejemplo §5.1): la nota es únicamente la frase libre
  tras `<id> <estado>`, preservada por `stop_at_positional`. Un `--nota` habría sido redundante.

Entregables: [`agent/cliparse.py`](../src/agent/cliparse.py) + [`tests/test_cliparse.py`](../tests/test_cliparse.py)
(28 casos) + 9 tests de integración en [`tests/test_cli.py`](../tests/test_cli.py) + `Command` dataclass con
`help <cmd>` / `<cmd> --help` + sección de README + bump `0.3.0 → 0.4.0`. Suite completa en verde.
