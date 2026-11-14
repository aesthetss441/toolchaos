"""Run an agent once per fault and classify what happened."""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Callable, Iterable

from .core import KINDS, Fault, Session


@dataclasses.dataclass
class Outcome:
    fault: Fault
    status: str  # recovered | degraded | crashed | not_triggered
    detail: str = ""


def targets_in(cassette: str | Path) -> list[str]:
    """Names of the dependencies recorded in a cassette, in first-call order."""
    import json
    names: list[str] = []
    for line in Path(cassette).read_text(encoding="utf-8").splitlines():
        if line.strip():
            name = json.loads(line)["name"]
            if name not in names:
                names.append(name)
    return names


def resilience_matrix(
    run: Callable[[Session], Any],
    cassette: str | Path,
    targets: Iterable[str] | None = None,
    kinds: Iterable[str] = KINDS,
    persistent: bool = False,
    same: Callable[[Any, Any], bool] = lambda answer, baseline: answer == baseline,
) -> list[Outcome]:
    """Replay ``run`` under every (target, kind) fault.

    ``run`` builds the agent from ``session.wrap(...)``-ed dependencies and returns
    its final answer. Outcomes:

    - recovered: same answer as the fault-free run (per ``same``)
    - degraded: finished, but with a different answer (e.g. an explicit fallback)
    - crashed: an exception escaped the agent
    - not_triggered: the agent never made the call the fault was aimed at
    """
    baseline = run(Session(cassette, mode="replay"))
    outcomes = []
    for target in targets or targets_in(cassette):
        for kind in kinds:
            fault = Fault(target, kind, every=persistent)
            session = Session(cassette, mode="replay", faults=[fault])
            try:
                answer = run(session)
            except Exception as exc:
                outcomes.append(Outcome(fault, "crashed", f"{type(exc).__name__}: {exc}"[:120]))
                continue
            if not session.injected:
                outcomes.append(Outcome(fault, "not_triggered"))
            elif same(answer, baseline):
                outcomes.append(Outcome(fault, "recovered"))
            else:
                outcomes.append(Outcome(fault, "degraded", str(answer)[:120]))
    return outcomes


_MARK = {"recovered": "✅ recovered", "degraded": "⚠️ degraded", "crashed": "❌ crashed", "not_triggered": "– n/a"}


def to_markdown(outcomes: list[Outcome]) -> str:
    """Targets as rows, fault kinds as columns."""
    kinds = list(dict.fromkeys(o.fault.kind for o in outcomes))
    rows = list(dict.fromkeys(o.fault.target for o in outcomes))
    cell = {(o.fault.target, o.fault.kind): _MARK[o.status] for o in outcomes}
    lines = ["| dependency | " + " | ".join(kinds) + " |", "|---|" + "---|" * len(kinds)]
    for target in rows:
        lines.append(f"| `{target}` | " + " | ".join(cell.get((target, k), "") for k in kinds) + " |")
    return "\n".join(lines)
