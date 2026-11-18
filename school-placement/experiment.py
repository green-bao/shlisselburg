"""Run the A/B/C experiment after the manual choices have been recorded."""
from __future__ import annotations

import argparse
from copy import deepcopy
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re

import engine
from search import search

ROOT = Path(__file__).resolve().parent
LABELS = {"A": "А", "B": "Б", "C": "В"}
MODE_LABELS = {"walk": "Пешком", "drive": "На автомобиле"}


def validate_preregistration(record: dict) -> dict:
    """Validate the recorded manual scenarios and their reasons."""
    if not isinstance(record, dict):
        raise ValueError("Предварительный выбор должен быть объектом JSON.")
    for field in ("recorded_at",):
        if not isinstance(record.get(field), str) or not record[field].strip():
            raise ValueError(f"Укажите поле {field} предварительного выбора.")
    if not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", record["recorded_at"]):
        raise ValueError("Дата выбора должна иметь формат ГГГГ.ММ.ДД.")
    datetime.strptime(record["recorded_at"], "%Y.%m.%d")
    modes = record.get("modes", ["walk", "drive"])
    if (not isinstance(modes, list) or not modes or any(type(mode) is not str for mode in modes)
            or len(set(modes)) != len(modes) or any(mode not in engine.MODES for mode in modes)):
        raise ValueError("Режимы эксперимента должны быть walk и/или drive без повторов.")
    manual = record.get("manual")
    if not isinstance(manual, dict):
        raise ValueError("Сначала зафиксируйте ручные варианты A и B и объясните их выбор.")
    for key, count, capacity in (("A", 1, 600), ("B", 2, 300)):
        item = manual.get(key)
        if not isinstance(item, dict) or not isinstance(item.get("basis"), str) or not item["basis"].strip():
            raise ValueError(f"Для варианта {LABELS[key]} требуется объяснение ручного выбора.")
        additions = item.get("additions")
        if not isinstance(additions, list) or len(additions) != count:
            raise ValueError(f"Для варианта {LABELS[key]} требуется школ: {count}.")
        ids = []
        for addition in additions:
            if not isinstance(addition, dict):
                raise ValueError("Для школы нужны номер квартала и вместимость.")
            block_id = addition.get("block_id")
            if type(block_id) is not int or not 0 <= block_id < 97:
                raise ValueError("Номер квартала должен быть целым числом от 0 до 96.")
            if type(addition.get("capacity")) is not int or addition["capacity"] != capacity:
                raise ValueError(f"Вместимость каждой школы варианта {LABELS[key]} должна быть {capacity} мест.")
            ids.append(block_id)
        if len(set(ids)) != len(ids):
            raise ValueError("Для варианта Б выберите два разных квартала.")
    clean = deepcopy(record)
    clean["modes"] = modes
    return clean


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _block_changes(original: dict, result: dict) -> dict:
    base = {row["block_id"]: row for row in original["blocks"]}
    return {
        "improved": [row["block_id"] for row in result["blocks"]
                     if row["supplied"] > base[row["block_id"]]["supplied"]],
        "worsened": [row["block_id"] for row in result["blocks"]
                     if row["supplied"] < base[row["block_id"]]["supplied"]],
        "residual_deficit": [row["block_id"] for row in result["blocks"] if row["deficit"] > 0],
    }


def _write_comparison_csv(path: Path, original: dict, results: dict) -> None:
    base = {row["block_id"]: row for row in original["blocks"]}
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(["Режим", "Вариант", "Квартал", "Население, чел.", "Потребность, мест",
                         "Вместимость исходная, мест", "Вместимость сценария, мест",
                         "Обеспечено исходно, мест", "Обеспечено в сценарии, мест",
                         "Дефицит исходный, мест", "Дефицит сценария, мест",
                         "Сокращение дефицита, мест", "Обеспеченность исходная, доля",
                         "Обеспеченность сценария, доля"])
        for key, result in results.items():
            for row in result["blocks"]:
                source = base[row["block_id"]]
                writer.writerow([MODE_LABELS[result["mode"]], LABELS[key], row["block_id"],
                                 row["population"], row["demand"], source["capacity"], row["capacity"],
                                 source["supplied"], row["supplied"], source["deficit"], row["deficit"],
                                 source["deficit"] - row["deficit"], source["provision"], row["provision"]])


def run_experiment(preregistration: Path, output: Path) -> dict:
    """Persist the supplied manual choice before calculating any automatic C."""
    source_bytes = preregistration.read_bytes()
    record = validate_preregistration(json.loads(source_bytes.decode("utf-8-sig")))
    # Preserve the decision and its exact source hash before the first search.
    output.mkdir(parents=True, exist_ok=True)
    decision = {"preregistration": record,
                "source_sha256": hashlib.sha256(source_bytes).hexdigest()}
    _write_json(output / "manual_choices.json", decision)
    bundle = {"manual_choices": decision, "modes": {}}
    saved = []
    for mode in record["modes"]:
        original = engine.baseline(mode)
        variants = {key: engine.calculate(mode, record["manual"][key]["additions"])
                    for key in ("A", "B")}
        # The two manual results are saved before revealing the automatic result.
        _write_json(output / f"manual_{mode}.json", {"baseline": original, "variants": variants})

        def progress(snapshot):
            print(f"{MODE_LABELS[mode]}: {snapshot['checked']}/{snapshot['total']}", flush=True)

        automatic = search(mode, 600, progress)
        _write_json(output / f"ranking_{mode}.json", automatic)
        with (output / f"ranking_{mode}.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream, delimiter=";")
            writer.writerow(["Квартал", "Остаточный дефицит, мест", "Сокращение дефицита, мест", "Ошибка"])
            writer.writerows([row["block_id"], row["deficit"], row["deficit_reduction"], row.get("error", "")]
                             for row in automatic["ranking"])
        if automatic["complete"]:
            chosen = automatic["best_block_ids"][0]
            variants["C"] = engine.calculate(mode, [{"block_id": chosen, "capacity": 600}])
            ranked = next(row for row in automatic["ranking"] if row["block_id"] == chosen)
            if variants["C"]["totals"]["deficit"] != ranked["deficit"]:
                raise RuntimeError("Ручная проверка автоматического результата не совпала с перебором.")
        changes = {key: _block_changes(original, result) for key, result in variants.items()}
        bundle["modes"][mode] = {"baseline": original, "variants": variants,
                                "automatic": automatic, "changes": changes}
        _write_comparison_csv(output / f"comparison_{mode}.csv", original, variants)
        for key, result in variants.items():
            prefix = ""
            basis = (record["manual"][key]["basis"] if key in ("A", "B") else
                     "Автоматический выбор по наибольшему сокращению суммарного дефицита. "
                     "Среди равноценных лучших кварталов показан квартал с наименьшим номером.")
            saved.append({"name": f"{prefix}{LABELS[key]} - {MODE_LABELS[mode].lower()}",
                          "basis": basis, "result": result})
    _write_json(output / "experiment.json", bundle)
    _write_json(output / "saved_scenarios.json", {"scenarios": saved})
    _write_report(output / "Эксперимент.md", bundle)
    return bundle


def _ids(values: list[int]) -> str:
    return ", ".join(map(str, values)) if values else "нет"


def _write_report(path: Path, bundle: dict) -> None:
    record = bundle["manual_choices"]["preregistration"]
    lines = ["# Размещение школ в Шлиссельбурге", "",
             "Сравниваются исходное состояние и три варианта размещения новых школ. Во всех вариантах добавлено 600 мест.", "",
             f"Дата фиксации ручного выбора: {record['recorded_at']}.", ""]
    lines += ["Ручные варианты А и Б выбраны по исходной карте.", ""]
    lines += [
             f"Вариант А: одна школа на 600 мест. {record['manual']['A']['basis']}", "",
             f"Вариант Б: две школы по 300 мест. {record['manual']['B']['basis'].replace('два размещения могут расширить доступность', 'две школы в разных частях города могут повысить доступность')}", "",
             "Оба ручных выбора сохранены до автоматического поиска.", "",
             "Вариант В: одна школа на 600 мест в квартале, выбранном автоматически по наибольшему сокращению суммарного дефицита мест на всей территории. Пеший и автомобильный режимы рассматриваются отдельно.", ""]
    for mode, data in bundle["modes"].items():
        lines += [f"## {MODE_LABELS[mode]}", "", "| Вариант | Размещение | Вместимость сети, мест | Остаточный дефицит, мест | Сокращение дефицита, мест |",
                  "|---|---|---:|---:|---:|"]
        base = data["baseline"]["totals"]
        lines += [f"| Исходное состояние | Существующие школы | {base['capacity']} | {base['deficit']} | 0 |"]
        for key, result in data["variants"].items():
            placement = "; ".join(f"квартал {item['block_id']}: {item['capacity']} мест" for item in result["additions"])
            total = result["totals"]
            lines += [f"| {LABELS[key]} | {placement} | {total['capacity']} | {total['deficit']} | {total['deficit_reduction']} |"]
        lines += [""]
        automatic = data["automatic"]
        if automatic["complete"]:
            lines += [f"Перебор завершён: {automatic['checked']} из {automatic['total']} кварталов, ошибок нет. Равноценные лучшие кварталы: {_ids(automatic['best_block_ids'])}.",
                      "При нескольких лучших кварталах в строке варианта В показан квартал с наименьшим номером; остальные дают такое же сокращение дефицита.", ""]
            manual_id = record["manual"]["A"]["additions"][0]["block_id"]
            match = "входит" if manual_id in automatic["best_block_ids"] else "не входит"
            lines += [f"Квартал {manual_id}, выбранный вручную для варианта А, {match} в набор равноценных лучших кварталов для одной школы.", ""]
            gain = max(result["totals"]["deficit_reduction"] for result in data["variants"].values())
            winners = [LABELS[key] for key, result in data["variants"].items() if result["totals"]["deficit_reduction"] == gain]
            lines += [(f"Во всех трёх вариантах дефицит сократился на {gain} мест." if len(winners) == 3 else
                       f"Наибольшее сокращение дефицита среди А, Б и В: {gain} мест; варианты: {', '.join(winners)}."), ""]
        else:
            lines += [f"Перебор неполный: попыток {automatic['checked']} из {automatic['total']}, успешно {automatic['succeeded']}, ошибок {len(automatic['errors'])}. Лучший вариант среди всех кварталов не установлен.", ""]
        grouped = {}
        for key, changes in data["changes"].items():
            signature = (tuple(changes["improved"]), tuple(changes["residual_deficit"]), tuple(changes["worsened"]))
            grouped.setdefault(signature, []).append(key)
        for signature, keys in grouped.items():
            changes = data["changes"][keys[0]]
            improvement = (f"обеспеченность повысилась в кварталах {_ids(changes['improved'])}"
                           if changes["improved"] else "обеспеченность кварталов осталась исходной")
            residual = (f"дефицит сохранился в кварталах {_ids(changes['residual_deficit'])}"
                        if changes["residual_deficit"] else "остаточного дефицита нет")
            label = f"Вариант {LABELS[keys[0]]}" if len(keys) == 1 else f"Варианты {', '.join(LABELS[key] for key in keys)}"
            lines += [f"{label}: {improvement}; {residual}."]
            if changes["worsened"]:
                lines += [f"При перераспределении мест обеспеченность снизилась в кварталах {_ids(changes['worsened'])}."]
        lines += [""]
        values = [result["totals"]["deficit"] for result in data["variants"].values()]
        if len(values) == 3 and len(set(values)) == 1:
            if any(data["changes"][key]["improved"] != data["changes"]["A"]["improved"] for key in data["changes"]):
                lines += ["При одинаковом общем результате улучшения распределяются по разным кварталам."]
            lines += [""]
    lines += ["## Данные и пределы вывода", "",
              "Население рассчитано по зданиям и оценке жилой площади во второй практике. Его величина зависит от полноты OpenStreetMap и допущений об использовании зданий. Два существующих корпуса одной школы имеют принятые вместимости 934 и 683 места по сведениям сайтов образовательных организаций; дата состояния этих сведений не установлена.", "",
              "Использован competitive_provision библиотеки BlocksNet 1.0.0a9 с исходными матрицами времени, 120 местами на 1000 жителей, порогом 15 минут, self_supply=True и max_depth=1. Это параметры учебной модели. Пешеходная сеть и автомобильная сеть дают разные результаты, поэтому режимы сравниваются отдельно. Автомобильный расчёт не учитывает пробки и поиск парковки.", "",
              "Автоматический перебор оценивает одну школу на 600 мест. Результат двух школ по 300 мест сопоставлен отдельно. Размещение по кварталам требует последующего выбора участка и градостроительного обоснования строительства.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Эксперимент после фиксации ручного выбора А и Б.")
    parser.add_argument("--choices", type=Path, required=True,
                        help="JSON: recorded_at (ГГГГ.ММ.ДД), modes и manual.A/manual.B с additions и basis")
    parser.add_argument("--output", type=Path, default=ROOT / "results" / "experiment")
    arguments = parser.parse_args()
    try:
        run_experiment(arguments.choices, arguments.output)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Ошибка: {exc}\n")


if __name__ == "__main__":
    main()
