"""Detector registry.

`LEAK_RULE` maps each injectable corpus leak to the rule that is supposed to
catch it. The eval uses it to score detectors *specifically*: a run only counts
as recall if the detector for the planted leak fired, not merely if something
fired. Without that, a noisy detector that fires on everything would score 100%.
"""

from __future__ import annotations

from ..model.events import OrderEvent
from ..model.roundtrip import RoundTrip
from .base import Leak
from .disposition import Disposition, Martingale
from .execution import StopMigration
from .selection import SymbolCompetence
from .sizing import LeverageDrag
from .tilt import Revenge
from .timing import SessionPerformance, strengths

ALL_DETECTORS = (
    Disposition(),
    Martingale(),
    Revenge(),
    SessionPerformance(),
    LeverageDrag(),
    StopMigration(),
    SymbolCompetence(),
)

LEAK_RULE = {
    "disposition": "disposition_effect",
    "martingale": "averaging_down",
    "revenge": "revenge_trading",
    "night": "session_performance",
    "overleverage": "leverage_drag",
    "stop_migration": "stop_migration",
    "symbol_leak": "symbol_selection",
}


def run_all(
    trips: list[RoundTrip], orders: list[OrderEvent] | None = None,
) -> list[Leak]:
    """Every detector, worst-costing leak first.

    `orders` is optional and defaults to nothing, so every existing call site
    keeps working unchanged. Only detectors that declare `wants_orders = True`
    receive it -- the `Detector` protocol still reads `run(trips)`, the other
    six detectors are untouched, and a caller with no order stream (a CSV
    export that carried only fills, say) gets silence from the detectors that
    need one rather than a guess from the ones that do not.
    """
    out: list[Leak] = []
    for det in ALL_DETECTORS:
        if getattr(det, "wants_orders", False):
            out.extend(det.run(trips, orders or []))
        else:
            out.extend(det.run(trips))
    out.sort(key=lambda leak: leak.cost if leak.cost is not None else 0.0)
    return out


__all__ = ["ALL_DETECTORS", "LEAK_RULE", "run_all", "strengths", "Leak"]
