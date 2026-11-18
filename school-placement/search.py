"""Exhaustive, independent single-school placement against the original inputs."""
from __future__ import annotations

from copy import deepcopy
from typing import Callable

import engine


def search(mode: str, capacity: int, progress_callback: Callable | None = None) -> dict:
    """Attempt every block and report all ties, including individual failures.

    ``checked`` counts attempted variants, including errors. ``complete`` means
    that every variant succeeded. Intermediate and incomplete snapshots expose
    ``best_calculated_block_ids`` only; they do not establish a global winner.
    The callback receives a separate snapshot after initialization and each try.
    """
    if type(capacity) is not int or capacity <= 0:
        raise ValueError("Вместимость школы должна быть положительным целым числом.")
    original = engine.baseline(mode)
    ids = [row["block_id"] for row in original["blocks"]]
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("В исходном наборе отсутствуют кварталы или повторяются их номера.")
    base = original["totals"]
    ranking: list[dict] = []
    errors: list[dict] = []

    def snapshot() -> dict:
        successful = sorted((row for row in ranking if row.get("error") is None),
                            key=lambda row: (-row["deficit_reduction"], row["block_id"]))
        failures = sorted((row for row in ranking if row.get("error") is not None),
                          key=lambda row: row["block_id"])
        best = []
        if successful:
            gain = successful[0]["deficit_reduction"]
            best = [row["block_id"] for row in successful if row["deficit_reduction"] == gain]
        complete = len(ranking) == len(ids) and not errors
        return deepcopy({
            "mode": mode, "capacity": capacity, "total": len(ids),
            "checked": len(ranking), "succeeded": len(successful),
            "baseline_deficit": base["deficit"], "ranking": successful + failures,
            "best_block_ids": best if complete else [],
            "best_calculated_block_ids": best, "errors": errors,
            "complete": complete,
        })

    def publish() -> None:
        if progress_callback is not None:
            progress_callback(snapshot())

    publish()
    for block_id in ids:
        try:
            # A new one-school list is passed on every iteration. The current
            # interface scenario never enters this calculation.
            result = engine.calculate(mode, [{"block_id": block_id, "capacity": capacity}])
            totals = result["totals"]
            if (result["mode"] != mode or totals["added_capacity"] != capacity
                    or totals["capacity"] != base["capacity"] + capacity
                    or totals["population"] != base["population"]
                    or totals["demand"] != base["demand"]):
                raise RuntimeError("Вариант изменил исходные условия или накопил вместимость других вариантов.")
            deficit = totals["deficit"]
            if type(deficit) is not int or not 0 <= deficit <= base["demand"]:
                raise RuntimeError("Расчёт вернул недопустимый остаточный дефицит мест.")
            ranking.append({"block_id": block_id, "deficit": deficit,
                            "deficit_reduction": base["deficit"] - deficit})
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            errors.append({"block_id": block_id, "error": message})
            ranking.append({"block_id": block_id, "deficit": None,
                            "deficit_reduction": None, "error": message})
        publish()
    return snapshot()
