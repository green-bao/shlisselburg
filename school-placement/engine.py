"""School provision using the saved practice 2 data and its BlocksNet method."""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from importlib.metadata import version
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
from blocksnet.analysis.provision import competitive_provision
from blocksnet.config import service_types_config
from loguru import logger

DATA = Path(__file__).resolve().parent / "data"
MODES = ("walk", "drive")
FIELDS = ("demand", "capacity", "demand_left", "demand_within", "demand_without",
          "capacity_left", "capacity_within", "capacity_without", "provision_strong", "provision_weak")


def _read_json(name: str) -> dict:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _inputs():
    if version("blocksnet") != "1.0.0a9":
        raise RuntimeError("Для расчёта требуется BlocksNet версии 1.0.0a9.")
    geometry = _read_json("blocks.geojson")
    props = [feature["properties"] for feature in geometry["features"]]
    blocks = pd.DataFrame(props).set_index("block_id")
    if not blocks.index.is_unique or blocks.index.tolist() != list(range(97)):
        raise ValueError("Нарушен состав 97 исходных кварталов.")
    for field in ("population", "capacity"):
        values = blocks[field].to_numpy(dtype=float)
        if not np.isfinite(values).all() or (values < 0).any():
            raise ValueError(f"Неверные исходные значения {field}.")
    config = service_types_config["school"]
    if int(config["demand"]) != 120 or int(config["accessibility"]) != 15:
        raise RuntimeError("Настройка school отличается от исходного расчёта практики 2.")
    matrices = {}
    for mode in MODES:
        payload = _read_json(f"matrix_{mode}.geojson")
        if payload.get("time_unit") != "min" or payload.get("mode") != mode:
            raise ValueError("Матрица должна содержать время выбранного режима в минутах.")
        rows = {}
        for feature in payload["features"]:
            properties = feature["properties"]
            origin = int(properties["block_id"])
            if origin in rows:
                raise ValueError("В матрице повторяется квартал отправления.")
            rows[origin] = {int(target): np.inf if value is None else float(value)
                            for target, value in properties["travel_times_min"].items()}
            if set(rows[origin]) != set(blocks.index):
                raise ValueError("Столбцы матрицы не соответствуют кварталам.")
        if set(rows) != set(blocks.index):
            raise ValueError("Строки матрицы не соответствуют кварталам.")
        matrix = pd.DataFrame.from_dict(rows, orient="index").reindex(index=blocks.index, columns=blocks.index)
        values = matrix.to_numpy()
        if np.isnan(values).any() or (values < 0).any() or (np.diag(values) != 0).any():
            raise ValueError("Недопустимые значения времени в матрице.")
        matrices[mode] = matrix
    return blocks, matrices


def _mode(mode: str) -> str:
    if mode not in MODES:
        raise ValueError("Выберите режим walk или drive.")
    return mode


def _validate_additions(additions) -> list[dict]:
    if not isinstance(additions, (list, tuple)):
        raise ValueError("Школы следует передать списком кварталов и вместимостей.")
    ids = _inputs()[0].index
    clean = []
    for addition in additions:
        if not isinstance(addition, dict):
            raise ValueError("Для школы нужны номер квартала и вместимость.")
        block_id, capacity = addition.get("block_id"), addition.get("capacity")
        if type(block_id) is not int or block_id not in ids:
            raise ValueError("Номер квартала должен быть целым числом от 0 до 96.")
        if type(capacity) is not int or capacity <= 0:
            raise ValueError("Вместимость школы должна быть положительным целым числом.")
        clean.append({"block_id": block_id, "capacity": capacity})
    return clean


def _run(mode: str, additions: list[dict]) -> tuple[pd.DataFrame, pd.DataFrame]:
    blocks, matrices = _inputs()
    working = blocks[["population", "capacity"]].copy(deep=True)
    for addition in additions:
        working.at[addition["block_id"], "capacity"] += addition["capacity"]
    logger.disable("blocksnet")
    provision, links = competitive_provision(working, matrices[mode], accessibility=15,
                                             demand=120, self_supply=True, max_depth=1)
    if not np.array_equal(provision.demand, provision.demand_left + provision.demand_within + provision.demand_without):
        raise RuntimeError("Нарушен баланс распределённой потребности.")
    if (provision[["demand_left", "capacity_left", "demand_within"]] < 0).any().any():
        raise RuntimeError("Получено отрицательное число мест.")
    if not provision.provision_strong.dropna().between(0, 1).all():
        raise RuntimeError("Получена недопустимая доля обеспеченности.")
    return provision, links


def _plain_number(value):
    if pd.isna(value) or not math.isfinite(float(value)):
        return None
    number = float(value)
    return int(number) if number.is_integer() else number


def _serialize(mode: str, additions: list[dict], provision: pd.DataFrame) -> dict:
    blocks = _inputs()[0]
    rows = []
    for block_id, row in provision.iterrows():
        record = {"block_id": int(block_id), "population": _plain_number(blocks.at[block_id, "population"]),
                  "demand": int(row.demand), "capacity": int(row.capacity),
                  "supplied": int(row.demand_within), "deficit": int(row.demand_left),
                  "provision": _plain_number(row.provision_strong)}
        # Preserve the complete original provision fields for export and verification.
        record.update({field: _plain_number(row[field]) for field in FIELDS})
        rows.append(record)
    added = sum(item["capacity"] for item in additions)
    deficit = int(provision.demand_left.sum())
    original_deficit = int(blocks[f"{mode}_demand_left"].sum())
    demand = int(provision.demand.sum())
    supplied = int(provision.demand_within.sum())
    return {"mode": mode, "additions": deepcopy(additions), "blocks": rows,
            "totals": {"population": int(blocks.population.sum()), "demand": demand,
                       "capacity": int(provision.capacity.sum()), "supplied": supplied,
                       "deficit": deficit, "added_capacity": added,
                       "deficit_reduction": original_deficit - deficit,
                       "provision": supplied / demand if demand else None}}


@lru_cache(maxsize=2)
def _original(mode: str) -> dict:
    provision, _ = _run(mode, [])
    expected = pd.read_csv(DATA / f"provision_{mode}.csv", index_col="block_id")
    if not expected.index.equals(provision.index):
        raise RuntimeError("Идентификаторы исходного расчёта не совпадают.")
    for field in FIELDS:
        actual = provision[field].to_numpy(dtype=float)
        reference = expected[field].to_numpy(dtype=float)
        if not np.allclose(actual, reference, rtol=1e-14, atol=1e-14, equal_nan=True):
            raise RuntimeError(f"Исходный расчёт {mode} не воспроизводит поле {field} практики 2.")
    return _serialize(mode, [], provision)


def baseline(mode: str = "walk") -> dict:
    """Return a fresh independent copy of the verified original provision."""
    return deepcopy(_original(_mode(mode)))


def calculate(mode: str, additions: list[dict]) -> dict:
    """Recompute territory-wide provision from original inputs for one scenario."""
    mode = _mode(mode)
    clean = _validate_additions(additions)
    baseline(mode)  # Stop if this environment cannot reproduce the original result.
    if not clean:
        return baseline(mode)
    provision, _ = _run(mode, clean)
    return _serialize(mode, clean, provision)


def geometry_data() -> dict:
    return _read_json("blocks.geojson")


def metadata() -> dict:
    manifest = _read_json("manifest.json")
    return {"city": "Шлиссельбург", "blocksnet": "1.0.0a9", "block_count": 97,
            "parameters": {"demand_per_1000": 120, "threshold_min": 15,
                           "self_supply": True, "max_depth": 1},
            "matrix_direction": "Строка - квартал жителей, столбец - квартал школы; время в минутах.",
            "population_note": "Население рассчитано по зданиям и оценке жилой площади практики 2; при отсутствии населения принято 30 м² на человека.",
            "capacity_note": "Существуют два корпуса одной школы в кварталах 3 и 24: 934 и 683 места. Это предоставленные значения сайтов образовательных организаций, дата их состояния не установлена; вместимость не оценивалась по площади зданий.",
            "parameter_note": "120 мест на 1000 жителей и 15 минут - учебные параметры модели; нормативное соответствие не проверяется.",
            "source": manifest["source"], "assumptions": manifest["assumptions"],
            "limitations": manifest["limitations"],
            "placement_note": "Приложение сравнивает размещение по кварталам. Выбор участка и возможность строительства требуют отдельного градостроительного обоснования."}
