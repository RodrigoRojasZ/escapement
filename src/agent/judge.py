"""Verifier adversarial: un juez independiente refuta el refactor antes de abrir el PR.

Complementa `verify.py` (tests + API-diff). Esos detectan que algo ROMPE, pero no un cambio
de comportamiento SUTIL que igual pasa los tests (código sin cobertura). El juez es otra
invocación del executor configurable (Claude por defecto) con instrucción de REFUTAR: busca
regresiones y cambios observables en el diff. El PR se abre solo si tests+API ok Y el juez no
encuentra riesgo -> sube la calidad de lo que llega a revisión. Fail-open: DESCONOCIDO no bloquea.
"""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass, field

JUDGE_SYSTEM = (
    "Eres un revisor de código senior, escéptico y conciso. Tu trabajo es REFUTAR: encontrar "
    "cualquier cambio de comportamiento observable, bug o regresión introducidos por un refactor "
    "que debía preservar el comportamiento. No elogies; solo reporta riesgos concretos."
)

JUDGE_PROMPT = """Un agente refactorizó código con esta directiva: {directiva}

El refactor DEBÍA preservar el comportamiento observable (mismos resultados, misma API pública).
Revisa el diff y decide si introdujo algún cambio de comportamiento, bug o regresión.

Responde SOLO con un JSON en una línea, sin texto extra:
{{"veredicto": "SEGURO" | "RIESGOSO", "problemas": ["descripción concreta", ...]}}
- SEGURO: el cambio preserva el comportamiento; problemas = [].
- RIESGOSO: hay evidencia concreta de un cambio de comportamiento o bug; lista cada uno.
Sé conservador: marca RIESGOSO solo con evidencia concreta en el diff, no por estilo.

DIFF:
{diff}
"""


@dataclass
class Judgment:
    verdict: str  # "SEGURO" | "RIESGOSO" | "DESCONOCIDO"
    issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True salvo que el juez haya refutado con evidencia (solo RIESGOSO bloquea)."""
        return self.verdict != "RIESGOSO"


def _parse(out: str) -> Judgment:
    """Extrae el JSON del veredicto de la salida del juez (robusto a texto alrededor)."""
    match = re.search(r"\{.*\}", out or "", re.DOTALL)
    if not match:
        return Judgment("DESCONOCIDO")
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return Judgment("DESCONOCIDO")
    verdict = str(data.get("veredicto", "DESCONOCIDO")).upper()
    if verdict not in ("SEGURO", "RIESGOSO"):
        verdict = "DESCONOCIDO"
    issues = [str(p).strip() for p in data.get("problemas", []) if str(p).strip()]
    return Judgment(verdict, issues)


def _vote(juicios: list[Judgment]) -> Judgment:
    """Agrega N veredictos en uno por voto de mayoría (Fase 0: verificación robusta).

    Solo votan los veredictos concretos (SEGURO/RIESGOSO); los DESCONOCIDO se abstienen. El
    resultado es SEGURO solo con mayoría ESTRICTA de seguros; empate o mayoría riesgosa ->
    RIESGOSO (conservador). Sin ningún voto concreto -> DESCONOCIDO (fail-open, como un juez mudo).
    """
    validos = [j for j in juicios if j.verdict in ("SEGURO", "RIESGOSO")]
    if not validos:
        return Judgment("DESCONOCIDO")
    riesgosos = [j for j in validos if j.verdict == "RIESGOSO"]
    if len(validos) - len(riesgosos) > len(riesgosos):  # seguros > riesgosos
        return Judgment("SEGURO")
    issues = list(dict.fromkeys(p for j in riesgosos for p in j.issues))  # dedup, orden estable
    return Judgment("RIESGOSO", issues)


def judge_diff(
    diff: str,
    directiva: str,
    *,
    timeout: float = 300,
    max_chars: int = 12000,
    voters: int | None = None,
) -> Judgment:
    """Somete el diff a ``voters`` jueces adversariales y agrega por voto. DESCONOCIDO no bloquea.

    Con ``voters=1`` (default vía config) es un juez único: el comportamiento previo, sin cuota
    extra. Con N impar >1, N muestras independientes del mismo juez votan por mayoría
    (self-consistency): reduce la varianza de un veredicto aislado a costa de N× la cuota.

    Args:
        diff: salida de ``git diff`` del refactor a juzgar.
        directiva: la instrucción original, como contexto para el juez.
        timeout: segundos máximos de espera a cada juez.
        max_chars: recorte del diff (diffs enormes -> prompt acotado; se juzga el prefijo).
        voters: nº de jueces que votan. None (default) toma ``config.JUDGE_VOTERS`` (env
            ``AGENT_JUDGE_VOTERS``, default 1). Se fuerza a >=1.
    """
    if not (diff or "").strip():
        return Judgment("SEGURO")  # sin cambios, nada que refutar
    from agent import config, executors

    n = max(1, config.JUDGE_VOTERS if voters is None else int(voters))
    prompt = f"{JUDGE_SYSTEM}\n\n" + JUDGE_PROMPT.format(directiva=directiva, diff=diff[:max_chars])
    # El juez sólo razona sobre el diff (texto); corre en un tmp aislado, no toca el repo.
    with tempfile.TemporaryDirectory() as tmp:
        juicios = [
            _parse(
                executors.run_agent(
                    prompt,
                    cwd=tmp,
                    timeout=timeout,
                    mode="read",
                    model=config.model_for("verificar"),
                )[1]
            )
            for _ in range(n)
        ]
    return juicios[0] if n == 1 else _vote(juicios)
