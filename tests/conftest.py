"""Aislamiento global de la suite: el journal de eventos nunca toca el de la máquina real.

Deuda #15. ``bus.publish(journal=True)`` escribe en ``config.EVENTS`` —el journal real,
``data/events.jsonl``— y la suite lo llama por todos lados: los tests de ``run_plan`` solos dejaban
cientos de eventos falsos (``"repo": "repo"``, planes de laboratorio) mezclados con la telemetría
de verdad, que es justo lo que `escapement eventos` y el bloque de plan de `estado` leen para
diagnosticar una corrida. Ruido indistinguible del real y crecimiento sin techo (``data/`` está
gitignorado, así que nadie lo notaba en el diff).

El fixture es **autouse y sin opt-out**: apunta ``config.EVENTS`` a un archivo dentro del ``tmp_path``
de cada test, que pytest limpia solo. Como todo el código lee el atributo en el momento de publicar
(``config.EVENTS.open(...)``, nunca ``from agent.config import EVENTS``), redirigirlo aquí cubre
cualquier módulo sin tocarlos. Un test que quiera su propio journal puede seguir haciendo
``monkeypatch.setattr(config, "EVENTS", ...)``: corre después de este fixture y gana.
"""

from __future__ import annotations

import pytest

from agent import config


@pytest.fixture(autouse=True)
def _journal_aislado(tmp_path, monkeypatch):
    """Redirige el journal del bus a ``tmp_path`` durante TODOS los tests (deuda #15)."""
    monkeypatch.setattr(config, "EVENTS", tmp_path / "events.jsonl")
