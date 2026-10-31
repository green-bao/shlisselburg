"""Build a local, self-contained data dashboard from saved Shlisselburg results.

Only Python's standard library is used. This script neither downloads geodata nor
calls BlocksNet: all analytical values must already exist in the saved results.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
MODES = ("walk", "drive")
BASE_FIELDS = (
    "block_id", "site_area", "building_count", "population", "footprint_area", "build_floor_area",
    "living_area", "non_living_area", "school_count", "capacity", "residential", "business", "recreation",
    "industrial", "transport", "special", "agriculture", "land_use", "share", "fsi", "gsi", "mxi", "l", "osr",
    "share_living", "share_non_living", "spacematrix_cluster", "spacematrix_morphotype", "strelka_morphotype",
)
MODE_FIELDS = (
    "area_accessibility", "area_accessibility_status", "demand", "capacity", "demand_left",
    "demand_within", "demand_without", "capacity_left", "capacity_within", "capacity_without", "provision_strong",
    "provision_weak",
)


def read_json(path: Path) -> Any:
    """Read strict JSON, rejecting nonstandard NaN/Infinity literals."""
    def reject_constant(value: str) -> None:
        raise ValueError(f"Недопустимое значение {value}; сохраните его как null")

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=reject_constant)
    except (OSError, ValueError) as error:
        raise ValueError(f"Не удалось прочитать {path}: {error}") from error


def read_geojson(path: Path, geometry_types: set[str]) -> dict[str, Any]:
    collection = read_json(path)
    if not isinstance(collection, dict) or collection.get("type") != "FeatureCollection":
        raise ValueError(f"{path}: требуется GeoJSON FeatureCollection")
    if not isinstance(collection.get("features"), list):
        raise ValueError(f"{path}: отсутствует список features")
    for index, feature in enumerate(collection["features"]):
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise ValueError(f"{path}, объект {index}: требуется GeoJSON Feature")
        if not isinstance(feature.get("properties"), dict):
            raise ValueError(f"{path}, объект {index}: отсутствуют свойства properties")
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict) or geometry.get("type") not in geometry_types:
            raise ValueError(f"{path}, объект {index}: неожиданный тип геометрии")

        def check_coordinates(coordinates: Any) -> None:
            if not isinstance(coordinates, list) or not coordinates:
                raise ValueError(f"{path}, объект {index}: пустые или некорректные координаты")
            if isinstance(coordinates[0], (int, float)):
                if len(coordinates) < 2 or any(
                    not isinstance(item, (int, float)) or not math.isfinite(item) for item in coordinates
                ):
                    raise ValueError(f"{path}, объект {index}: некорректная координата")
                if not -180 <= coordinates[0] <= 180 or not -90 <= coordinates[1] <= 90:
                    raise ValueError(f"{path}, объект {index}: ожидаются координаты EPSG:4326")
            else:
                for child in coordinates:
                    check_coordinates(child)

        check_coordinates(geometry.get("coordinates"))
    return collection


def integer_id(value: Any, context: str) -> int:
    try:
        number = float(value)
        if isinstance(value, bool) or not math.isfinite(number) or not number.is_integer():
            raise ValueError
        return int(number)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{context}: идентификатор квартала должен быть целым числом") from error


def read_matrix(path: Path, block_ids: set[int]) -> dict[str, Any]:
    """Preserve matrix cells; positive infinity is encoded as a JSON null."""
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream)
        try:
            header = next(reader)
        except StopIteration as error:
            raise ValueError(f"{path}: пустая матрица") from error
        if not header or header[0] != "block_id":
            raise ValueError(f"{path}: первый столбец должен называться block_id")
        columns = [integer_id(value, str(path)) for value in header[1:]]
        if len(columns) != len(set(columns)) or set(columns) != block_ids:
            raise ValueError(f"{path}: столбцы матрицы не совпадают с кварталами blocks.geojson")
        origins: list[int] = []
        values: list[list[float | None]] = []
        for line_number, row in enumerate(reader, start=2):
            if not row:
                continue
            if len(row) != len(header):
                raise ValueError(f"{path}, строка {line_number}: неверное число столбцов")
            origins.append(integer_id(row[0], f"{path}, строка {line_number}"))
            matrix_row: list[float | None] = []
            for raw in row[1:]:
                try:
                    value = float(raw)
                except ValueError as error:
                    raise ValueError(f"{path}, строка {line_number}: время должно быть числом или inf") from error
                if math.isnan(value) or value < 0:
                    raise ValueError(f"{path}, строка {line_number}: время не может быть NaN или отрицательным")
                matrix_row.append(value if math.isfinite(value) else None)
            values.append(matrix_row)
        if len(origins) != len(set(origins)) or set(origins) != block_ids:
            raise ValueError(f"{path}: строки матрицы не совпадают с кварталами blocks.geojson")
    return {"origins": origins, "destinations": columns, "values": values, "unit": "min"}


def read_clusters(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not {"cluster", "l", "fsi", "mxi"}.issubset(reader.fieldnames or []):
            raise ValueError(f"{path}: требуются столбцы cluster, l, fsi, mxi")
        rows: list[dict[str, Any]] = []
        for row in reader:
            clean: dict[str, Any] = dict(row)
            clean["cluster"] = integer_id(row["cluster"], str(path))
            for key in ("l", "fsi", "mxi"):
                try:
                    value = float(row[key]) if row[key] else None
                except ValueError as error:
                    raise ValueError(f"{path}: {key} должно быть числом или пустым значением") from error
                clean[key] = value if value is not None and math.isfinite(value) else None
            rows.append(clean)
    return rows


def load_payload() -> dict[str, Any]:
    required = [
        "results/blocks.geojson", "results/manifest.json", "results/spacematrix_clusters.csv",
        "data/raw/boundary.geojson", "data/prepared/schools.geojson",
        "data/prepared/water.geojson", "data/raw/roads.geojson",
        *[f"results/matrices/{mode}.csv" for mode in MODES],
        *[f"results/provision_links_{mode}.geojson" for mode in MODES],
    ]
    missing = [name for name in required if not (HERE / name).is_file()]
    if missing:
        raise ValueError("Не хватает сохранённых результатов. Выполните ноутбук до конца:\n  " + "\n  ".join(missing))
    blocks = read_geojson(HERE / "results/blocks.geojson", {"Polygon", "MultiPolygon"})
    if not blocks["features"]:
        raise ValueError("results/blocks.geojson: нет кварталов для отображения")
    block_ids: set[int] = set()
    expected = set(BASE_FIELDS) | {f"{mode}_{field}" for mode in MODES for field in MODE_FIELDS}
    categorical = {"land_use", "spacematrix_morphotype", "strelka_morphotype"}
    categorical |= {f"{mode}_area_accessibility_status" for mode in MODES}
    for feature in blocks["features"]:
        properties = feature["properties"]
        missing_fields = sorted(expected - properties.keys())
        if missing_fields:
            raise ValueError(f"Квартал {properties.get('block_id')}: отсутствуют поля {', '.join(missing_fields)}")
        block_id = integer_id(properties["block_id"], "blocks.geojson")
        if block_id in block_ids:
            raise ValueError(f"blocks.geojson: повторный block_id {block_id}")
        properties["block_id"] = block_id
        block_ids.add(block_id)
        for field in expected - categorical - {"block_id"}:
            value = properties[field]
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                raise ValueError(f"Квартал {block_id}: {field} должно быть числом или null")
        for mode in MODES:
            if properties[f"{mode}_area_accessibility_status"] not in {"ok", "unreachable"}:
                raise ValueError(f"Квартал {block_id}: некорректный статус доступности {mode}")
    manifest = read_json(HERE / "results/manifest.json")
    manifest_fields = {"title", "city", "captured_at", "crs", "versions", "assumptions", "limitations", "stages", "summary"}
    if not isinstance(manifest, dict) or not manifest_fields.issubset(manifest):
        raise ValueError("results/manifest.json: неполный состав метаданных")
    for field in ("assumptions", "limitations"):
        if not isinstance(manifest[field], list) or any(not isinstance(item, str) for item in manifest[field]):
            raise ValueError(f"manifest.json: {field} должен быть списком строк")
    for field in ("versions", "stages", "summary"):
        if not isinstance(manifest[field], dict):
            raise ValueError(f"manifest.json: {field} должен быть словарём")
    schools = read_geojson(HERE / "data/prepared/schools.geojson", {"Point", "MultiPoint", "Polygon", "MultiPolygon"})
    for feature in schools["features"]:
        if not {"name", "capacity", "capacity_estimated"}.issubset(feature["properties"]):
            raise ValueError("schools.geojson: требуются поля name, capacity, capacity_estimated")
        capacity = feature["properties"]["capacity"]
        if capacity is not None and (isinstance(capacity, bool) or not isinstance(capacity, (int, float))):
            raise ValueError("schools.geojson: capacity должно быть числом или null")
        if not isinstance(feature["properties"]["capacity_estimated"], bool):
            raise ValueError("schools.geojson: capacity_estimated должно быть true или false")
    links = {}
    for mode in MODES:
        links[mode] = read_geojson(HERE / f"results/provision_links_{mode}.geojson", {"LineString"})
        for feature in links[mode]["features"]:
            properties = feature["properties"]
            if not {"source", "target", "value"}.issubset(properties):
                raise ValueError(f"Связи {mode}: требуются source, target, value")
            if any(integer_id(properties[key], f"Связи {mode}") not in block_ids for key in ("source", "target")):
                raise ValueError(f"Связи {mode}: ссылка на отсутствующий квартал")
            if isinstance(properties["value"], bool) or not isinstance(properties["value"], (int, float)) or properties["value"] < 0:
                raise ValueError(f"Связи {mode}: value должно быть неотрицательным числом")
    building_path = HERE / "data/prepared/buildings.geojson"
    locality_path = HERE / "data/raw/boundary_locality.geojson"
    return {
        "manifest": manifest,
        "blocks": blocks,
        "boundary": read_geojson(HERE / "data/raw/boundary.geojson", {"Polygon", "MultiPolygon"}),
        "locality": read_geojson(locality_path, {"Polygon", "MultiPolygon"}) if locality_path.is_file() else None,
        "water": read_geojson(HERE / "data/prepared/water.geojson", {"Polygon", "MultiPolygon"}),
        "roads": read_geojson(HERE / "data/raw/roads.geojson", {"Point", "MultiPoint", "LineString", "MultiLineString"}),
        "schools": schools,
        "buildings": read_geojson(building_path, {"Polygon", "MultiPolygon"}) if building_path.is_file() else None,
        "matrices": {mode: read_matrix(HERE / f"results/matrices/{mode}.csv", block_ids) for mode in MODES},
        "links": links,
        "clusters": read_clusters(HERE / "results/spacematrix_clusters.csv"),
    }


HTML = r'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>Шлиссельбург | Кварталы и доступность</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
:root{--ink:__UI_ink__;--muted:__UI_muted__;--line:__UI_grid__;--paper:__UI_panel__;--bg:__UI_background__;--accent:__UI_accent__;--accent-light:#eef4f8;--red:#a25252;--radius:12px}
*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:20px}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 "Segoe UI",Arial,sans-serif}button,input,select{font:inherit}button,select,summary{cursor:pointer}button,input,select{color:inherit}button{background:#fff;border:1px solid var(--line);border-radius:7px;padding:7px 11px}button:hover{background:#f3f5f6}button:disabled{opacity:.4;cursor:default}select,input{border:1px solid #d6dce0;border-radius:7px;padding:8px 10px;background:#fff;max-width:100%;min-width:0}button:focus-visible,input:focus-visible,select:focus-visible,a:focus-visible,summary:focus-visible{outline:3px solid #88b3ce;outline-offset:2px}a{color:var(--accent)}h1,h2,h3,p{margin:0}h1{font-size:29px;font-weight:600;letter-spacing:-.5px}h2{font-size:20px;font-weight:600;line-height:1.3}h3{font-size:16px;font-weight:600}small,.small{font-size:12px}label{display:grid;gap:5px;color:var(--muted);font-size:12px}label.check{display:inline-flex;align-items:center;gap:7px;font-size:12px}input[type=checkbox]{accent-color:var(--accent);margin:0;width:15px;height:15px}.shell{max-width:1500px;margin:auto;padding:0 28px}.topline{background:#fff;border-bottom:1px solid var(--line)}.topline .shell{min-height:50px;display:flex;justify-content:space-between;align-items:center;gap:20px}.brand{font-size:13px;font-weight:600}.brand span{font-weight:400;color:var(--muted);margin-left:12px}nav{display:flex;gap:19px;flex-wrap:wrap}nav a{font-size:12px;color:var(--muted);text-decoration:none}.hero{padding:25px 0 20px;display:flex;align-items:center;justify-content:space-between;gap:25px}.hero p{font-size:13px;color:var(--muted);margin-top:6px}.mode-panel{text-align:right;flex-shrink:0;max-width:360px}.mode-label{font-size:12px;color:var(--muted);margin-bottom:6px}.mode-switch{display:inline-flex;border:1px solid #d9dfe2;border-radius:8px;overflow:hidden}.mode-switch button{border:0;border-radius:0;background:#fff;padding:8px 14px;font-size:13px}.mode-switch button+button{border-left:1px solid #d9dfe2}.mode-switch button[aria-pressed=true]{color:#245677;background:#eaf1f5;font-weight:600}.mode-panel small{display:block;color:var(--muted);font-size:11px;margin-top:6px}.filters{display:flex;align-items:flex-end;gap:12px;flex-wrap:wrap;margin-bottom:18px;padding:14px 16px;background:#fff;border:1px solid var(--line);border-radius:10px}.filters label{flex:1;min-width:160px}.filters .info{font-size:12px;color:var(--muted);align-self:center;margin-left:auto}.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px;margin-bottom:24px}.card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px 19px}.card .label{font-size:12px;color:var(--muted)}.card .value{font-size:28px;font-weight:600;letter-spacing:-.5px;margin:5px 0}.card .note{font-size:11px;color:var(--muted);line-height:1.5}.section{margin-bottom:25px}.section-head{display:flex;justify-content:space-between;align-items:center;gap:20px;margin:24px 0 12px}.section-head p{color:var(--muted);font-size:12px;margin-top:6px;max-width:900px}.panel{background:#fff;border:1px solid var(--line);border-radius:var(--radius);overflow:hidden;min-width:0}.badge{display:inline-block;border:1px solid #dde7ed;border-radius:5px;padding:3px 8px;color:#4f6e82;background:#f4f7f9;font-size:11px;white-space:nowrap}.toolbar{display:flex;align-items:flex-end;gap:10px;flex-wrap:wrap;padding:13px 16px;border-bottom:1px solid var(--line)}.toolbar .wide,.toolbar .search{flex:1;min-width:180px}.toolbar .wide select{width:100%}.toolbar .info{font-size:11px;color:var(--muted);align-self:center}.map-layout{display:grid;grid-template-columns:minmax(0,1.8fr) minmax(310px,1fr);gap:20px;align-items:stretch}.map-column{position:relative}#map{height:600px;background:__MAP_mapBackground__}.legend{position:absolute;bottom:28px;left:12px;max-width:250px;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:#fffffff5;z-index:450;font-size:11px;pointer-events:none}.legend-title{font-weight:600;margin-bottom:6px}.legend-item{display:flex;align-items:center;gap:7px;margin:3px 0}.legend-note{color:var(--muted);font-size:10px;margin-top:5px}.swatch{display:inline-block;width:13px;height:9px;border:1px solid #0001;border-radius:2px;flex:none}.layer-toolbar{border-top:1px solid var(--line);border-bottom:0;padding:10px 14px}.map-unavailable{padding:25px;color:var(--muted)}.callout{padding:11px 15px;border-top:1px solid var(--line);font-size:11px;line-height:1.55;color:var(--muted)}.callout strong{color:#4b5c65;font-weight:600}.inspector{padding:20px;min-width:0}.inspector-top{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}.inspector-top button{font-size:11px;padding:5px 8px}.inspector .intro{font-size:17px;font-weight:600;color:var(--accent);margin-bottom:4px}.inspector .secondary{font-size:12px;color:var(--muted);margin-bottom:20px}.basic-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px 14px;margin:0 0 21px}.basic-grid dt{font-size:11px;color:var(--muted)}.basic-grid dd{margin:4px 0 0;font-size:20px;font-weight:550;font-variant-numeric:tabular-nums}.metric-highlight{border-top:1px solid var(--line);padding:16px 0;margin-bottom:10px}.metric-highlight .caption{font-size:12px;color:var(--muted)}.metric-highlight .number{text-align:left;font-size:28px;font-weight:600;letter-spacing:-.5px;margin:6px 0;line-height:1.25}.metric-highlight .explanation{font-size:11px;color:var(--muted);line-height:1.6}.inspector details{border-top:1px solid var(--line);margin-top:8px}.inspector summary{font-size:12px;font-weight:600;padding:13px 0}.all-attributes{max-height:340px;overflow:auto;padding-right:6px}.attribute-group{margin:0;font-size:11px;font-weight:600;color:#4b616e;padding:14px 0 5px}.detail-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(85px,.75fr);gap:15px;font-size:11px;padding:7px 0;border-bottom:1px solid #f0f1f2;align-items:start}.detail-row span{color:var(--muted)}.detail-row strong{font-weight:500;text-align:right;overflow-wrap:anywhere}.inspector-empty{min-height:430px;display:flex;justify-content:center;align-items:center;flex-direction:column;text-align:center;padding:30px 20px;gap:12px}.inspector-empty p{max-width:255px;color:var(--muted);font-size:13px;line-height:1.65}.charts{display:grid;grid-template-columns:1fr 1fr;gap:20px}.chart-panel{padding:18px 12px 7px;display:flex;flex-direction:column}.chart-panel h3,.chart-panel p{padding:0 7px}.chart-panel p{color:var(--muted);font-size:11px;margin-top:6px;min-height:35px}.chart{height:295px;width:100%;flex:0 0 295px;margin-top:auto}.table-wrap{overflow:auto;max-height:560px;max-width:100%;isolation:isolate}.data-table{width:100%;border-collapse:separate;border-spacing:0;font-size:12px;text-align:left;white-space:nowrap}.data-table th,.data-table td{border-right:1px solid #e0e5e8}.data-table th:last-child,.data-table td:last-child{border-right:0}.data-table th{position:sticky;top:0;z-index:2;background:#f6f8f9;border-bottom:1px solid #dce3e7;vertical-align:bottom;color:#526672;font-weight:600;padding:0}.data-table th button{display:block;border:0;border-radius:0;background:transparent;color:inherit;width:100%;padding:11px 13px;text-align:inherit;font-size:11px;min-width:125px;white-space:normal;line-height:1.4}.data-table th .unit{display:block;font-size:10px;font-weight:400;color:var(--muted)}.data-table th.number button{text-align:right}.data-table th.category{min-width:185px}.data-table th.id-col button{min-width:75px}.data-table td{padding:9px 13px;border-bottom:1px solid #eef0f2;max-width:270px;overflow:hidden;text-overflow:ellipsis}.data-table tr[data-id]{cursor:pointer}.data-table tr[data-id]:hover td{background:#f3f7fa}.data-table tr.selected td{background:#eaf2f7}.data-table .id-col{position:sticky;left:0;z-index:1;background:white;color:var(--accent);font-weight:600}.data-table th.id-col{left:0;z-index:3;background:#f6f8f9}.data-table .id-col{box-shadow:1px 0 0 #d5dfe5}.data-table.plain-table th{padding:11px 13px;min-width:125px}.data-table.plain-table th.id-col{min-width:145px}.number{text-align:right;font-variant-numeric:tabular-nums}.missing,.status{color:var(--muted);font-size:11px}.unreachable{color:var(--red);font-size:12px}.table-footer{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:11px 16px;border-top:1px solid var(--line);font-size:11px;color:var(--muted)}.pagination{display:flex;gap:7px;align-items:center}.pagination button{padding:4px 10px}.pagination label{display:flex;gap:6px;align-items:center}.pagination select{padding:4px 8px}.empty{padding:28px;color:var(--muted);font-size:13px}.matrix-intro{display:grid;grid-template-columns:1fr 1fr;gap:18px;padding:16px;border-bottom:1px solid var(--line)}.matrix-intro p{font-size:11px;color:var(--muted)}.matrix-stat{display:flex;gap:20px;flex-wrap:wrap;font-size:11px;color:var(--muted)}.matrix-stat strong{display:block;font-size:21px;color:var(--ink);font-weight:600}#matrix-table tbody tr{cursor:default}.matrix-table{max-height:410px}.matrix-table td{white-space:normal}#matrix-table th[aria-sort=ascending] button[data-sort]::after{content:" ↑"}#matrix-table th[aria-sort=descending] button[data-sort]::after{content:" ↓"}.disclosure{border:1px solid var(--line);border-radius:10px;background:#fff}.disclosure>summary{font-size:15px;font-weight:550;padding:14px 17px}.disclosure-body{padding:0 16px 16px}.compact-section{margin-bottom:12px}.compact-section .panel{border-radius:8px}.panel-body{padding:19px 21px}.panel-body p{font-size:12px;color:var(--muted);line-height:1.7}.cluster-table td,.cluster-table th{white-space:normal;min-width:90px}.cluster-table th{padding:10px 13px}footer{display:flex;justify-content:space-between;gap:20px;margin:28px 0;padding:18px 0;border-top:1px solid var(--line);font-size:11px;color:var(--muted)}footer p{max-width:950px}.error-banner{padding:14px;background:#fff6f6;border:1px solid #dfbcbc;border-radius:8px;color:#914d4d;margin-bottom:18px}.leaflet-tooltip{font:12px/1.5 "Segoe UI",Arial,sans-serif}.leaflet-popup-content{font:12px/1.5 "Segoe UI",Arial,sans-serif}.school-marker{background:#fff;border:2px solid __MAP_school__;border-radius:50%;width:13px!important;height:13px!important;box-shadow:0 1px 4px #0002}.leaflet-pane path[role=button]:focus{outline:none;stroke:__MAP_selection__;stroke-width:3}.leaflet-pane path[role=button]{cursor:pointer}[hidden]{display:none!important}

.school-times{border-top:1px solid var(--line);padding:15px 0 14px}.school-times h4{font-size:13px;font-weight:600;line-height:1.6;margin:0 0 8px}.school-time-list{margin:0}.school-time-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;align-items:baseline;padding:7px 0}.school-time-row dt{font-size:12px;color:var(--muted)}.school-time-row dd{margin:0;font-size:16px;font-weight:550;font-variant-numeric:tabular-nums}.school-time-note{font-size:11px;color:var(--muted);line-height:1.6;margin-top:7px}.metric-label{display:inline-flex;align-items:center;gap:5px}.help-button,.data-table th button.help-button{display:inline-flex;align-items:center;justify-content:center;position:relative;top:-.35em;flex:0 0 10px;width:10px;height:10px;min-width:10px;max-width:10px;margin-left:2px;padding:0;border:0;border-radius:0;background:transparent;color:#426b83;font-size:8px;font-weight:600;line-height:1;vertical-align:super;white-space:nowrap}.help-button::before{content:"";position:absolute;inset:-4px}.help-button:hover,.help-button[aria-expanded=true]{background:transparent;color:#164d70;text-decoration:underline}.help-button:focus-visible{outline:2px solid #88b3ce;outline-offset:2px}.column-head{display:flex;align-items:flex-start;gap:0}.data-table th.number .column-head{flex-direction:row-reverse}.data-table th .column-head>button:not(.help-button){flex:1;min-width:100px}.data-table th.id-col .column-head>button:not(.help-button){min-width:75px}.column-head>.help-button{margin:10px 7px 0 0}.data-table th.number .column-head>.help-button{margin:10px 0 0 7px}.header-text{flex:1;min-width:0;white-space:normal}.plain-table .column-head>.help-button,.cluster-table .column-head>.help-button{margin:0 0 0 7px}.plain-table th.number .column-head>.help-button,.cluster-table th.number .column-head>.help-button{margin:0 7px 0 0}.metric-help{position:fixed;z-index:2000;width:330px;max-width:calc(100vw - 20px);padding:14px 16px;border:1px solid #b8cbd7;border-radius:9px;background:#fff;box-shadow:0 6px 24px #24374624;font-size:13px;line-height:1.6;text-align:left;overflow:auto;max-height:calc(100vh - 20px)}.metric-help strong{display:block;margin-bottom:7px;font-size:14px;color:#294f67}.metric-help p{white-space:normal}.provision-note{border-bottom:1px solid var(--line);border-top:0}#provision-options{align-self:center;flex:none;font-size:12px}.land-share{font-size:12px;color:var(--muted);margin:4px 0 8px}
@media(min-width:1600px){#map{height:650px}}@media(max-width:1050px){.shell{padding:0 20px}.map-layout{grid-template-columns:minmax(0,1.4fr) minmax(280px,1fr);gap:14px}.inspector{padding:16px}.toolbar{padding:12px}.card{padding:14px}.card .value{font-size:25px}}
@media(max-width:760px){.shell{padding:0 14px}.topline .shell{display:block;padding-top:13px;padding-bottom:12px}.brand span{display:none}nav{margin-top:10px;gap:15px}.hero{display:block;padding:20px 0 17px}.hero h1{font-size:27px}.mode-panel{text-align:left;margin-top:15px}.filters{gap:10px;padding:12px}.filters label{min-width:130px;flex-basis:100%}.filters .info{margin-left:0}.cards{grid-template-columns:1fr 1fr;gap:10px;margin-bottom:19px}.card{padding:14px}.card .value{font-size:25px}.map-layout,.charts,.matrix-intro{grid-template-columns:minmax(0,1fr)}#map{height:470px}.inspector-empty{min-height:180px;padding:20px}.inspector{padding:18px}.section-head{align-items:flex-start}.section-head .badge{white-space:normal}.legend{max-width:210px;font-size:10px}.table-footer{display:block}.pagination{margin-top:8px;flex-wrap:wrap}.toolbar label{min-width:130px;flex:1}.toolbar button{font-size:11px}.map-toolbar .wide{flex-basis:100%}.chart{height:270px;flex-basis:270px}footer{display:block}footer>span{display:block;margin-top:9px}}
@media print{body{background:#fff}.shell{max-width:100%;padding:0}.topline,.filters,.mode-panel,.toolbar button,.pagination,.map-help,footer>span{display:none}.hero{padding:10px 0}.map-layout{grid-template-columns:1.6fr 1fr;gap:10px}#map{height:450px}.inspector{padding:12px}.basic-grid{gap:10px}.all-attributes{max-height:none}.charts{gap:10px}.card{padding:10px}.section{break-inside:avoid}.table-wrap{max-height:none;overflow:visible}.data-table{white-space:normal;font-size:9px}.data-table th{position:static}.data-table td{padding:5px}}
footer{flex-wrap:wrap}footer .norm-note{flex-basis:100%;max-width:100%;margin-top:6px;line-height:1.6;font-size:11px}
.metric-label{gap:0}
.help-button,.data-table th button.help-button{align-self:flex-start;top:-1px;flex:0 0 9px;width:9px;height:9px;min-width:9px;max-width:9px;margin-left:.5px;padding:0;border:1px solid #98a9b3;border-radius:50%;background:transparent;font-size:7px;font-weight:400;line-height:1;vertical-align:super}
.help-button:hover,.help-button[aria-expanded=true]{background:transparent;text-decoration:none}
.column-head{justify-content:flex-start;padding:11px 13px}
.data-table th.number .column-head{flex-direction:row;justify-content:flex-end}
.data-table th .column-head>button:not(.help-button),.data-table th.id-col .column-head>button:not(.help-button){flex:0 1 auto;min-width:0;width:auto;padding:0}
.header-text{flex:0 1 auto}
.column-head>.help-button,.data-table th.number .column-head>.help-button,.plain-table .column-head>.help-button,.cluster-table .column-head>.help-button,.plain-table th.number .column-head>.help-button,.cluster-table th.number .column-head>.help-button{margin:0 0 0 .5px}

.column-head,.data-table th.number .column-head{position:relative;display:block;padding:11px 13px}
.header-text{display:inline;position:relative;z-index:1;pointer-events:none}
.header-text>.help-button{pointer-events:auto}
.header-tail{display:inline-block;white-space:nowrap}
.header-tail>.help-button{pointer-events:auto}
.data-table th .column-head>button.header-sort,.data-table th.id-col .column-head>button.header-sort{position:absolute;inset:0;z-index:0;width:100%;height:100%;min-width:0;max-width:none;padding:0;border:0;background:transparent}
.header-sort>.sort-label{visibility:hidden}
.column-head>.unit{position:relative;z-index:1;pointer-events:none}
.data-table th.number .column-head{text-align:right}
.data-table th.category .column-head,.data-table th.id-col .column-head{text-align:left}

#model-limitations{font-size:12px;line-height:1.7;color:var(--muted)}#model-limitations summary,#model-limitations li{font-family:inherit;font-size:inherit;line-height:inherit;font-weight:400}#model-limitations code{font-size:1em}
</style>
</head>
<body>
<div class="topline"><div class="shell"><div class="brand">Шлиссельбург <span>Кварталы и доступность школ</span></div><nav aria-label="Разделы"><a href="#territory">Карта</a><a href="#indicators">Показатели</a><a href="#matrix">Время в пути</a><a href="#methods">Источники</a></nav></div></div>
<main class="shell">
<header class="hero"><div><h1 id="page-title">Шлиссельбург</h1><p>Кварталы, улицы, вода и здания: OpenStreetMap. Вместимость школ: сведения сайтов образовательных организаций.</p></div><div class="mode-panel"><div class="mode-label"><span id="mode-label">Способ передвижения</span><button type="button" class="help-button" data-help-key="mode_choice" aria-label="Пояснение: способ передвижения" aria-expanded="false" aria-controls="metric-help">?</button></div><div class="mode-switch" role="group" aria-labelledby="mode-label"><button type="button" data-mode="walk" aria-pressed="true">Пешком</button><button type="button" data-mode="drive" aria-pressed="false">На автомобиле</button></div><small id="mode-note">Пешая модель, скорость 5 км/ч</small></div></header>
<div id="startup-error" class="error-banner" hidden></div>
<div class="filters" role="group" aria-label="Фильтры кварталов"><label><span class="metric-label">Использование территории по OSM<button type="button" class="help-button" data-help-key="land_use" aria-label="Пояснение: использование территории по OSM" aria-expanded="false" aria-controls="metric-help">?</button></span><select id="land-filter"><option value="">Все типы</option></select></label><label for="morph-filter"><span class="metric-label"><span id="morph-filter-label">Морфотип Spacematrix</span><button type="button" class="help-button" data-help-key="spacematrix_morphotype" aria-label="Пояснение: морфотип Spacematrix" aria-expanded="false" aria-controls="metric-help">?</button></span><select id="morph-filter" aria-labelledby="morph-filter-label"><option value="">Все морфотипы</option></select></label><button type="button" id="reset-filters">Сбросить фильтры</button><span class="info" id="filter-count" aria-live="polite"></span></div>
<div class="cards" id="summary-cards"></div>
<section class="section" id="territory"><div class="section-head"><div><h2>Карта кварталов</h2><p>Нажмите на квартал, чтобы увидеть его показатели.</p></div><span class="badge" id="active-mode-label">Пешая доступность</span></div>
<div class="map-layout"><div class="panel"><div class="toolbar map-toolbar"><label class="wide">Показатель карты<select id="map-metric"></select></label><button type="button" id="fit-city">Кварталы</button><button type="button" id="fit-map">Вся территория</button><button type="button" id="fit-selected" hidden>Приблизить выбранный</button></div>
<div class="map-column"><div id="map" aria-label="Интерактивная карта кварталов"></div><div id="legend" class="legend"></div></div>
<div class="toolbar layer-toolbar"><label class="check"><input type="checkbox" id="layer-schools" checked>Школьные корпуса</label><label class="check" id="buildings-control"><input type="checkbox" id="layer-buildings">Здания</label><span class="info" id="map-count"></span></div><div class="callout" id="metric-description"></div></div>
<aside class="panel inspector" id="inspector" aria-live="polite" aria-label="Выбранный квартал"></aside></div></section>
<section class="section" aria-labelledby="charts-title"><div class="section-head"><div><h2 id="charts-title">Показатели застройки и школ</h2><p>Графики используют текущую выборку кварталов. Нажатие на точку выбирает квартал на карте.</p></div></div><div class="charts"><div class="panel chart-panel"><h3>Обеспеченность школами до 15 минут</h3><p id="chart-provision-note"></p><div class="chart" id="chart-provision"></div></div><div class="panel chart-panel"><h3>Плотность и застроенность</h3><p>Одна точка соответствует кварталу. Цвет показывает расчётную обеспеченность школами; пустой кружок означает, что обеспеченность не определена.</p><div class="chart" id="chart-density"></div></div></div></section>
<section class="section" id="indicators"><div class="section-head"><div><h2>Показатели кварталов</h2><p>Фильтры сверху действуют на карту, карточки, графики и таблицу. Нажатие на строку выбирает квартал. Оба формата сохраняют текущую выборку и показатели обоих режимов.</p></div><span class="badge" id="table-count"></span></div><div class="panel"><div class="toolbar"><label>Группа колонок<select id="column-group"><option value="overview">Основные</option><option value="geometry">Площади и здания</option><option value="land">Использование территории по OSM</option><option value="density">Плотность и морфотипы</option><option value="access">Доступность</option><option value="provision">Обеспеченность школами</option><option value="all">Все показатели режима</option><option value="both">Все показатели обоих режимов</option></select></label><label class="check" id="provision-options" hidden><input type="checkbox" id="provision-all">Все показатели школьного расчёта</label><button type="button" id="export-blocks">Скачать CSV</button><button type="button" id="export-geojson">Скачать GeoJSON</button></div><div class="callout provision-note" id="provision-summary" hidden></div><div class="table-wrap"><table class="data-table" id="blocks-table"><thead></thead><tbody></tbody></table></div><div class="table-footer"><span id="blocks-page-status"></span><div class="pagination"><button type="button" id="blocks-prev">Назад</button><span id="blocks-page"></span><button type="button" id="blocks-next">Далее</button><label>Строк<select id="blocks-size"><option>8</option><option>20</option><option>50</option><option>100</option><option value="all">Все</option></select></label></div></div><div class="callout">Пустые исходные значения обозначены как «Не определено». Нулевое значение показывается числом 0. Недостижимость в сети имеет отдельное обозначение.</div></div></section>
<section class="section compact-section" id="school-flows"><details class="disclosure"><summary>Распределение школьных мест между кварталами</summary><div class="disclosure-body"><p class="small" id="flow-note" style="margin-bottom:12px"></p><div class="table-wrap"><table class="data-table plain-table" id="provision-links-table"><thead><tr><th data-help-header="flow_source" scope="col" class="id-col">Квартал жителей</th><th data-help-header="flow_target" scope="col" class="number">Квартал школы</th><th data-help-header="flow_value" scope="col" class="number">Выделено мест</th><th data-help-header="matrix_time" scope="col" class="number">Время между кварталами<span class="unit">мин</span></th></tr></thead><tbody></tbody></table></div><p class="small" style="margin-top:12px">Показаны связи из кварталов жителей в текущей выборке. Места для жителей того же квартала входят в общую обеспеченность, но не в эту таблицу.</p></div></details></section>
<section class="section compact-section" id="matrix"><details class="disclosure"><summary>Время в пути между кварталами</summary><div class="disclosure-body"><div class="panel"><div class="toolbar"><label>Квартал отправления<select id="matrix-origin"></select></label><label>Квартал назначения<select id="matrix-destination"><option value="">Все кварталы</option></select></label><label>Показать<select id="matrix-filter"><option value="all">Все назначения</option><option value="reachable">Достижимые</option><option value="within">В пределах 15 минут</option><option value="unreachable">Недостижимые</option></select></label><button type="button" id="export-origin">CSV выбранной строки</button><button type="button" id="export-matrix">CSV полной матрицы</button></div><div class="matrix-intro"><div><p style="margin-bottom:8px">По всем назначениям из выбранного квартала отправления</p><div class="matrix-stat" id="matrix-stats"></div></div><p>Время указано в минутах. 0 мин: отправление и назначение представлены одной расчётной точкой квартала; время перемещения внутри квартала не рассчитывается. «Недостижимо»: в выбранной сети нет маршрута или допустимого подключения к ней; в CSV обозначается <code>inf</code>. Порог 15 минут принят в учебной модели BlocksNet.</p></div><div class="table-wrap matrix-table"><table class="data-table" id="matrix-table"><thead><tr><th data-help-header="matrix_destination" class="id-col"><button data-sort="destination">Квартал назначения</button></th><th data-help-header="matrix_time" class="number"><button data-sort="time">Время<span class="unit">мин</span></button></th><th data-help-header="population" class="number"><button data-sort="population">Население назначения<span class="unit">чел.</span></button></th><th data-help-header="capacity" class="number"><button data-sort="capacity">Вместимость школ назначения<span class="unit">мест</span></button></th></tr></thead><tbody></tbody></table></div><div class="table-footer"><span id="matrix-page-status"></span><div class="pagination"><button type="button" id="matrix-prev">Назад</button><span id="matrix-page"></span><button type="button" id="matrix-next">Далее</button></div></div></div></div></details></section>
<section class="section compact-section" id="morphology"><details class="disclosure"><summary>Характеристики групп Spacematrix</summary><div class="disclosure-body"><div class="panel"><div class="table-wrap"><table class="data-table cluster-table" id="clusters-table"></table></div></div></div></details></section>
<section class="section" id="model-conclusions"><div class="section-head"><h2>Краткие выводы</h2></div><div class="panel panel-body" id="conclusions-paragraphs"></div></section>

<section class="section" id="methods"><div class="section-head"><h2>Данные и метод</h2></div><div class="panel panel-body"><p>Источник зданий, улиц, воды и землепользования: <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap contributors, ODbL</a>. Кварталы выделены по улицам, железным дорогам и воде. Геометрия существующих школьных корпусов взята из геоданных учебной работы «Моделирование территории». Вместимость учебных корпусов принята по сведениям, опубликованным на сайтах образовательных организаций. Характеристики кварталов и доступность рассчитаны автоматически. Расчёт выполнен в BlocksNet 1.0.0a9 по <a href="https://github.com/aimclub/blocksnet/blob/v1.0.0.a9/examples/pipeline.ipynb" target="_blank" rel="noopener noreferrer">учебному примеру</a>. Пояснения и формулы приведены в <a href="shlisselburg.ipynb">shlisselburg.ipynb</a>.</p><p style="margin-top:10px">Для пешего и автомобильного режимов используется сеть из сохранённых данных OpenStreetMap; время указано в минутах.</p><p style="margin-top:10px">Пешая скорость: 5 км/ч. Автомобильные скорости заданы по типам дорог; пробки и поиск парковки не учитываются. Время считается между точками кварталов. Порог 15 минут и расчётный показатель 120 школьных мест на 1000 жителей приняты из конфигурации библиотеки. Население оценивается по жилой площади. Вместимости корпусов 683 и 934 места приняты по сведениям сайтов образовательных организаций. Дата этих сведений не указана.</p><p id="source-dates-note" style="margin-top:10px">Дата получения ранее сохранённых отдельных слоёв границ, дорог, железных дорог, воды, использования территории и зданий не зафиксирована. Дата внизу страницы относится только к общей XML-выборке OSM.</p><details id="model-limitations" style="margin-top:12px"><summary>Допущения и ограничения расчёта</summary><ul><li>Жилое назначение определяется по явным тегам OSM. Объекты с тегом <code>building=yes</code> не включены в оценку жилой площади и населения; в садоводствах <code>allotments</code> постоянное население не назначается.</li><li>Оценка населения использует 30 м² расчётной жилой площади на человека. При отсутствии этажности принят один этаж. Нулевое модельное население не подтверждает отсутствие реальных жителей.</li><li>Сеть охватывает территорию исследования и пояс 2 км. Дальний объезд за его пределами может отсутствовать в модели. Водный и общественный транспорт не учитываются.</li><li>Прямые подходы от точек кварталов к сети проверены на пересечение воды. Проходимость через заборы, здания и закрытые территории не проверена.</li></ul></details><p style="margin-top:10px">При отсутствии сетевого пути хотя бы к одному кварталу среднее время до всех кварталов равно ∞. Время по отдельным направлениям приведено в матрице.</p></div></section>

<footer><p>Локальная учебная модель Шлиссельбурга. Данные OSM: © OpenStreetMap contributors/ODbL.</p><span id="footer-date"></span><p id="school-norm-note" class="norm-note">Параметры учебной модели BlocksNet: 120 школьных мест на 1000 расчётных жителей и порог 15 минут. Расчёт не является проверкой нормативного соответствия. Для обычных общеобразовательных школ Шлиссельбурга нормативные показатели: минимум 91 место на 1000 постоянных жителей и территориальная доступность не более 500 м. Основания: <a href="https://arch.lenobl.ru/media/uploads/userfiles/2025/08/27/%D0%9C%D0%9D%D0%93%D0%9F.docx" target="_blank" rel="noopener noreferrer">Местные нормативы градостроительного проектирования (постановление Правительства Ленинградской области от 04.12.2017 № 525), п. 2.1.2</a>; <a href="https://arch.lenobl.ru/media/uploads/userfiles/2025/08/27/%D0%A0%D0%9D%D0%93%D0%9F.docx" target="_blank" rel="noopener noreferrer">Региональные нормативы градостроительного проектирования (постановление Правительства Ленинградской области от 22.03.2012 № 83), приложение 3</a>.</p></footer>
</main>
<div id="metric-help" class="metric-help" role="tooltip" hidden><strong id="metric-help-title"></strong><p id="metric-help-text"></p></div>
<script id="dashboard-data" type="application/json">__DASHBOARD_DATA__</script>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<script>
"use strict";
const DATA=JSON.parse(document.getElementById("dashboard-data").textContent);
const $=id=>document.getElementById(id);
const esc=value=>String(value??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const finite=value=>typeof value==="number"&&Number.isFinite(value);
const fmt=(value,digits=2)=>!finite(value)?"Не определено":value!==0&&Math.abs(value)<0.5*10**(-digits)?value.toLocaleString("ru-RU",{maximumSignificantDigits:3}):value.toLocaleString("ru-RU",{maximumFractionDigits:digits});
function russianForm(value,forms){const n=Math.abs(Number(value));if(!Number.isInteger(n))return forms[1];const tail=n%100,last=n%10;return tail>=11&&tail<=14?forms[2]:last===1?forms[0]:last>=2&&last<=4?forms[1]:forms[2];}
function places(value){return finite(value)?fmt(value,0)+" "+russianForm(value,["место","места","мест"]):"Не определено";}
const groupNames={geometry:"Площади и здания",land:"Использование территории по OSM",density:"Плотность и морфотипы",access:"Транспортная доступность",provision:"Обеспеченность школами",other:"Дополнительные показатели"};
const META={};
function metric(key,label,unit,description,group,kind="number"){META[key]={key,label,unit,description,group,kind};}
metric("block_id","Номер квартала","ID","Уникальный номер квартала в этой модели.","geometry","id");
metric("site_area","Площадь квартала","м²","Площадь квартала, рассчитанная в принятой метрической системе координат.","geometry");
metric("building_count","Число зданий","шт.","Число зданий, отнесённых к кварталу по их расположению.","geometry");
metric("is_living","Число жилых зданий","шт.","Число зданий, которые модель отнесла к жилым.","geometry");
metric("number_of_floors","Сумма этажностей зданий","этажей","Сумма числа этажей всех зданий квартала. Средняя этажность рассчитывается отдельно как L: суммарная поэтажная площадь / суммарное пятно зданий.","geometry");
metric("population","Расчётное население","чел.","Расчётное число жителей квартала. Оценивается по жилой площади зданий при 30 м² на человека.","geometry");
metric("footprint_area","Суммарное пятно зданий","м²","Сумма площадей контуров зданий квартала на плане.","geometry");
metric("build_floor_area","Суммарная поэтажная площадь","м²","Суммарная расчётная площадь всех этажей зданий квартала.","geometry");
metric("living_area","Жилая площадь","м²","Суммарная расчётная жилая площадь зданий квартала. Для зданий, отнесённых к жилым, принято 90% поэтажной площади.","geometry");
metric("non_living_area","Прочая расчётная площадь","м²","Суммарная поэтажная площадь минус расчётная жилая площадь. Для зданий без известного назначения эта разность не подтверждает нежилое использование.","geometry");
metric("school_count","Школьные корпуса","шт.","Количество существующих учебных корпусов по вручную собранным данным. Корпуса одной школы учитываются отдельно.","provision");
metric("capacity","Вместимость школ в квартале","мест","Суммарное число мест в школьных корпусах квартала. Вместимость принята по сведениям, опубликованным на сайтах образовательных организаций.","provision");
const landNames={residential:"Жилая",business:"Деловая",recreation:"Рекреационная",industrial:"Промышленная",transport:"Транспортная",special:"Специальная",agriculture:"Сельскохозяйственная",unknown:"Не определено",undefined:"Не определено",none:"Не определено"};
const landFunctionNames={residential:"жилой",business:"деловой",recreation:"рекреационной",industrial:"промышленной",transport:"транспортной",special:"специальной",agriculture:"сельскохозяйственной"};
for(const [key,name] of Object.entries(landFunctionNames))metric(key,key==="agriculture"?"Доля сельскохозяйственной категории OSM":"Доля "+name+" функции","%",key==="agriculture"?"Доля площади квартала в сельскохозяйственной группе OSM landuse. В неё входят allotments (садоводства) и farmland (сельскохозяйственные участки); значение 1 соответствует 100%.":"Доля площади квартала, отнесённая к "+name+" функции по зонам OSM landuse; значение 1 соответствует 100%.","land","percent");
metric("land_use","Использование территории по OSM","категория","Категория, занимающая наибольшую площадь среди распознанных зон OSM landuse. Она может занимать небольшую часть квартала; её доля показана отдельно. Нераспознанная часть квартала не относится к этой категории автоматически.","land","category");
metric("share","Доля преобладающей функции","%","Доля полной площади квартала, занятая категорией из поля land_use. Категория выбрана среди распознанных зон; значение 1 соответствует 100%.","land","percent");
metric("fsi","FSI: интенсивность застройки","м²/м²","Сколько квадратных метров суммарной поэтажной площади приходится на 1 м² квартала. Формула: суммарная поэтажная площадь / площадь квартала.","density");
metric("gsi","GSI: застроенность","%","Какая доля площади квартала занята основаниями зданий. Формула: пятно зданий / площадь квартала × 100%. Результат ограничен диапазоном от 0 до 100%.","density","percent");
metric("mxi","MXI: доля жилой площади","%","Какая часть суммарной поэтажной площади отнесена к жилью. Формула: жилая площадь / суммарная поэтажная площадь × 100%. Результат ограничен диапазоном от 0 до 100%.","density","percent");
metric("l","L: расчётная этажность","этажей","Расчётная средняя этажность зданий квартала. Формула: суммарная поэтажная площадь / пятно зданий. Если результат формулы меньше 1, используется 1 этаж.","density");
metric("osr","OSR: открытое пространство","м²/м²","Сколько незастроенной площади квартала приходится на 1 м² суммарной поэтажной площади. Формула: (площадь квартала минус пятно зданий) / суммарная поэтажная площадь.","density");
metric("share_living","Жилая площадь к пятну зданий","м²/м²","Жилая площадь / пятно зданий. Отношение может превышать 1.","density");
metric("share_non_living","Прочая площадь к пятну зданий","м²/м²","Прочая расчётная площадь / пятно зданий. Отношение может превышать 1.","density");
metric("spacematrix_cluster","Кластер Spacematrix","ID","Номер группы кварталов, выделенной по средней этажности, интенсивности застройки и доле жилой площади.","density","category");
metric("spacematrix_morphotype","Расчётный морфотип Spacematrix","категория","В BlocksNet метод Spacematrix присваивает тип застройки группе кварталов со сходной этажностью, интенсивностью застройки и долей жилой площади. «Стрелка» определяет тип каждого квартала отдельно по фиксированным правилам. На оба результата влияет полнота сведений OSM о назначении и этажности зданий.","density","category");
metric("strelka_morphotype","Расчётный морфотип Strelka","категория","В реализации BlocksNet классификация «Стрелки» определяет тип застройки каждого квартала по фиксированным правилам, используя этажность, интенсивность застройки и долю жилой площади. Spacematrix сначала объединяет похожие кварталы в группы и определяет тип по характеристикам группы. На оба результата влияет полнота сведений OSM о назначении и этажности зданий.","density","category");
metric("area_accessibility","Среднее время до всех кварталов","мин","Для каждого исходного квартала отдельно усредняются времена пути до всех 97 кварталов с весами по площади кварталов назначения. В среднее также включено время до расчётной точки самого исходного квартала, равное 0 минут. При отсутствии пути хотя бы к одному кварталу среднее равно ∞. Кластеры Spacematrix в этом расчёте не участвуют.","access");
metric("area_accessibility_status","Статус среднего времени","статус","ok: конечное среднее; unreachable: среднее не является конечным из-за недостижимости части территории в сети.","access","category");
metric("demand","Потребность в местах","мест","Расчётная потребность: 120 школьных мест на 1000 расчётных жителей, с округлением вверх. При нулевом модельном населении она равна 0; это не подтверждает отсутствие реальных жителей.","provision");
metric("mode_capacity","Вместимость в расчёте","мест","Вместимость школьных объектов квартала, переданная в расчёт распределения для выбранного режима.","provision");
metric("demand_left","Необеспечённая потребность","мест","Часть расчётной потребности, для которой модель не распределила места. На результат влияют вместимость школ и время пути: свободные места в другом квартале могут быть недоступны в пределах 15 минут.","provision");
metric("demand_within","Выделено до 15 мин","мест","Сколько мест модель выделила для расчётной потребности жителей квартала в пределах 15 минут, включая места внутри самого квартала.","provision");
metric("demand_without","Выделено свыше 15 мин","мест","Школьные места, выделенные для расчётной потребности жителей квартала за пределами 15 минут, если выбранная глубина поиска это допускает.","provision");
metric("capacity_left","Свободные места после распределения","мест","Вместимость школ квартала, оставшаяся после модельного распределения.","provision");
metric("capacity_within","Места для других кварталов до 15 минут","мест","Школьные места, выделенные моделью жителям других кварталов в пределах 15 минут. Места для жителей самого квартала в этот показатель не входят.","provision");
metric("capacity_without","Места для других кварталов свыше 15 минут","мест","Школьные места, выделенные моделью жителям других кварталов за пределами 15 минут, если глубина поиска это допускает. Места для жителей самого квартала в этот показатель не входят.","provision");
metric("provision_strong","Обеспеченность в пределах 15 минут","%","Какая часть расчётной потребности покрыта местами в пределах 15 минут. Выделено мест до 15 минут / расчётная потребность в школьных местах × 100%. При нулевой потребности доля не определяется: в формуле получается деление на 0.","provision","percent");
metric("provision_weak","Обеспеченность с учётом дальних связей","%","Какая часть расчётной потребности покрыта всеми распределёнными школьными местами, включая места за пределами 15 минут. В текущем сценарии дальние места не распределяются: глубина поиска равна 1 (max_depth=1). Поэтому эта доля совпадает с долей покрытия до 15 минут. При нулевой потребности доля не определяется: в формуле получается деление на 0.","provision","percent");
const requiredModeFields=["area_accessibility","area_accessibility_status","demand","capacity","demand_left","demand_within","demand_without","capacity_left","capacity_within","capacity_without","provision_strong","provision_weak"];
const modeFields=requiredModeFields;
const modeKeys=m=>modeFields.filter(key=>key!=="area_accessibility_status").map(key=>m+"_"+key);
const modeTitle={walk:"Пешком",drive:"На автомобиле"};
const optionalBaseFields=["is_living","number_of_floors"];
const baseKeys=Object.keys(META).filter(key=>!modeFields.includes(key)||key==="capacity").filter(key=>key!=="mode_capacity").filter(key=>!optionalBaseFields.includes(key)||DATA.blocks.features.some(feature=>Object.hasOwn(feature.properties,key)));
const schema=()=>[...baseKeys,...modeKeys(mode)];
function info(key,includeMode=showBoth()){const match=key.match(/^(walk|drive)_(.+)$/);if(match){const result=META[match[2]==="capacity"?"mode_capacity":match[2]];return{...result,key,label:result.label+(includeMode?" ("+modeTitle[match[1]].toLowerCase()+")":"")};}return META[key]||{key,label:key,unit:"значение",description:"Дополнительное поле сохранённого результата.",group:"other",kind:typeof rows[0]?.[key]==="number"?"number":"category"};}
function showBoth(){return $("column-group")?.value==="both";}

const DRIVE_COMPARISON="В автомобильном расчёте для сопоставления с пешим принят порог 15 минут.";
const HELP={
    mode_choice:{label:"Способ передвижения",unit:"",description:"Режим расчёта времени в пути и распределения школьных мест. Скорость ходьбы, включая подходы к автомобильной сети: 5 км/ч. "+DRIVE_COMPARISON},
    school_times:{label:"Время до кварталов со школами",unit:"мин",description:"Время между расчётными точками кварталов с учётом подходов к дорожной сети. Положение входов в школы не учитывается. В автомобильном режиме время подходов рассчитывается при скорости ходьбы 5 км/ч."},
    flow_source:{label:"Квартал жителей",unit:"ID",description:"Квартал, для расчётной потребности жителей которого модель выделила школьные места."},
    flow_target:{label:"Квартал школы",unit:"ID",description:"Квартал со школой, места которой модель выделила жителям другого квартала."},
    flow_value:{label:"Выделено мест",unit:"мест",description:"Количество школьных мест, распределённых моделью из квартала школы для расчётной потребности жителей указанного квартала."},
    matrix_destination:{label:"Квартал назначения",unit:"ID",description:"Квартал, до которого рассчитано время пути из выбранного квартала отправления. Можно выбрать один квартал назначения или все."},
    matrix_time:{label:"Время между кварталами",unit:"мин",description:"Время по выбранной транспортной сети между расчётными точками кварталов. 0 минут: отправление и назначение представлены одной точкой квартала; время перемещения внутри квартала не рассчитывается. «Недостижимо» (inf в CSV): не найден маршрут или подход к сети без пересечения воды. Положение дома и входа в школу не учитывается."}
};
function helpInfo(key){const meta=HELP[key]||info(key,false);return /^drive_provision_(strong|weak)$/.test(key)?{...meta,description:meta.description+" "+DRIVE_COMPARISON}:meta;}
function helpButton(key){const meta=helpInfo(key);return '<button type="button" class="help-button" data-help-key="'+esc(key)+'" aria-label="Пояснение: '+esc(meta.label)+'" aria-expanded="false" aria-controls="metric-help">?</button>';}
function headerMarkup(content,helpKey,sortAttributes=""){
    const holder=document.createElement("div");holder.innerHTML=content;
    const unit=holder.querySelector(".unit"),unitHTML=unit?unit.outerHTML:"";
    if(unit)unit.remove();
    const text=holder.textContent,direction=text.match(/\s+([↑↓])\s*$/),label=direction?text.slice(0,direction.index):text;
    const parts=label.match(/^(.*?)(\S+)\s*$/s)||["","",label],hide=sortAttributes?' aria-hidden="true"':"";
    const sort=sortAttributes?'<button type="button" class="header-sort" '+sortAttributes+'><span class="sort-label">'+content+'</span></button>':"";
    const visible='<span'+hide+'>'+esc(parts[1])+'</span><span class="header-tail"><span'+hide+'>'+esc(parts[2])+'</span>'+(helpKey?helpButton(helpKey):"")+(direction?'<span'+hide+'> '+esc(direction[1])+'</span>':"")+'</span>';
    return '<div class="column-head">'+sort+'<span class="header-text">'+visible+'</span>'+unitHTML+'</div>';
}

let activeHelp=null;
function closeHelp(){if(activeHelp){activeHelp.setAttribute("aria-expanded","false");activeHelp.removeAttribute("aria-describedby");}activeHelp=null;$("metric-help").hidden=true;}
function positionHelp(){
    if(!activeHelp||!activeHelp.isConnected){closeHelp();return;}
    const box=$("metric-help"),rect=activeHelp.getBoundingClientRect();
    if(rect.bottom<0||rect.top>innerHeight||rect.right<0||rect.left>innerWidth){closeHelp();return;}
    const margin=10,width=box.offsetWidth,height=box.offsetHeight;
    const left=Math.max(margin,Math.min(rect.right-width,innerWidth-width-margin));
    const below=rect.bottom+8,top=below+height<=innerHeight-margin?below:Math.max(margin,rect.top-height-8);
    box.style.left=left+"px";box.style.top=top+"px";
}
function openHelp(button){
    if(activeHelp===button){closeHelp();return;}
    closeHelp();button.scrollIntoView({block:"nearest",inline:"nearest",behavior:"instant"});activeHelp=button;const meta=helpInfo(button.dataset.helpKey);
    $("metric-help-title").textContent=meta.label+(meta.unit&&meta.unit!=="категория"?" ("+meta.unit+")":"");
    $("metric-help-text").textContent=meta.description;$("metric-help").hidden=false;
    button.setAttribute("aria-expanded","true");button.setAttribute("aria-describedby","metric-help");positionHelp();
}
function bindHelp(){
    document.querySelectorAll("th[data-help-header]").forEach(th=>{const button=th.querySelector("button");const attributes=button?[...button.attributes].filter(a=>a.name!=="type"&&a.name!=="class").map(a=>a.name+'="'+esc(a.value)+'"').join(" "):"";th.innerHTML=headerMarkup(button?button.innerHTML:th.innerHTML,th.dataset.helpHeader,attributes);});
    document.addEventListener("click",event=>{const button=event.target.closest(".help-button");if(button){openHelp(button);return;}if(!event.target.closest("#metric-help"))closeHelp();});
    document.addEventListener("keydown",event=>{if(event.key==="Escape"&&activeHelp){event.preventDefault();const button=activeHelp;closeHelp();if(button.isConnected)button.focus();}});
    document.addEventListener("focusin",event=>{if(activeHelp&&event.target!==activeHelp&&!event.target.closest("#metric-help"))closeHelp();});
    window.addEventListener("resize",positionHelp);document.addEventListener("scroll",()=>{if(activeHelp)positionHelp();},true);
}
function noDemandRatio(row,key){const match=key.match(/^(walk|drive)_provision_(strong|weak)$/);return !!match&&row[match[1]+"_demand"]===0&&(row[key]===null||row[key]===undefined);}
function rowDisplay(row,key,withUnit=false){return noDemandRatio(row,key)?"Не определяется: потребность 0":display(row[key],key,withUnit);}

function morphotypeTitle(value){
    if(value===null||value===undefined)return"Не классифицировано";
    const special={
        "individual residential":"Модель индивидуальной застройки",
        "low-rise model":"Малоэтажная модель",
        "mid-rise":"Среднеэтажная модель",
        "mid-rise microdistrict":"Среднеэтажный микрорайон",
        "mid-rise block":"Среднеэтажная квартальная модель",
        "central model":"Модель центральной застройки",
        "high-rise soviet microdistrict":"Многоэтажный микрорайон советской модели",
        "high-rise modern microdistrict":"Многоэтажный микрорайон современной модели"
    };
    const text=String(value);
    if(Object.hasOwn(special,text))return special[text];
    const pieces=[];
    for(const[token,label]of[["high-mid-rise","Повышенная средняя этажность"],["low-rise","Малоэтажная"],["mid-rise","Среднеэтажная"],["high-rise","Многоэтажная"],["tower","Башенная"]]){
        if(text.startsWith(token)){pieces.push(label);break;}
    }
    if(text.includes("low-density"))pieces.push("низкая плотность");
    if(text.includes("high-density"))pieces.push("высокая плотность");
    if(text.includes("non-residential"))pieces.push("малая расчётная доля жилья");
    else if(text.includes("mixed-use"))pieces.push("смешанный тип");
    else if(text.includes("residential"))pieces.push("преимущественно жилой тип");
    return pieces.length?pieces.join(", "):text;
}
function translate(value,key){
    if(key.includes("morphotype"))return morphotypeTitle(value);
    if(value===null||value===undefined||value==="")return"Не определено";
    if(key==="land_use")return landNames[value]||String(value);
    if(key.endsWith("area_accessibility_status"))return value==="unreachable"?"Есть недостижимые назначения":value==="ok"?"Конечное среднее":String(value);
    return String(value);
}

function display(value,key,withUnit=false){const meta=info(key);if(key.includes("morphotype"))return translate(value,key);if(value===null||value===undefined)return"Не определено";if(meta.kind==="category")return translate(value,key);if(!finite(value))return esc(value);const result=meta.kind==="percent"?fmt(value*100,1)+"%":fmt(value,["чел.","мест","шт.","ID"].includes(meta.unit)?0:2);return result+(withUnit&&meta.kind!=="percent"&&meta.unit!=="ID"?" "+(meta.unit==="мест"?russianForm(value,["место","места","мест"]):meta.unit):"");}
const rows=DATA.blocks.features.map(f=>f.properties).sort((a,b)=>a.block_id-b.block_id);
const byId=new Map(rows.map(row=>[row.block_id,row]));
let mode="walk",selected=rows[0].block_id,blockPage=0,matrixPage=0,blockSort={key:"block_id",direction:1},matrixSort={key:"time",direction:1};
let hasSelection=false;
let map=null,blockLayer=null,boundaryLayer=null,schoolLayer=null,buildingLayer=null,mapScale=null;
let landLayer=null,waterLayer=null,roadsLayer=null,cityBounds=null;
const MAP_STYLE=__MAP_STYLE__;
const colors=MAP_STYLE.numeric,categoryPalettes=MAP_STYLE.categories;
const isUnreachable=(row,key)=>key.endsWith("area_accessibility")&&row[key]===null&&row[key.replace(/area_accessibility$/,"area_accessibility_status")]==="unreachable";
function cell(row,key){const value=row[key];if(noDemandRatio(row,key))return'<span class="status">Не определяется: потребность 0</span>';if(isUnreachable(row,key))return'<span class="unreachable" title="Среднее время до всех кварталов бесконечно: в сети есть недостижимые кварталы назначения.">∞</span>';if(value===null||value===undefined)return'<span class="missing">'+(key.includes("morphotype")?"Не классифицировано":"Не определено")+'</span>';return esc(display(value,key));}
function sumKnown(items,key){const values=items.map(item=>item[key]).filter(finite);return{value:!items.length?0:values.length?values.reduce((sum,n)=>sum+n,0):null,known:values.length};}
function downloadFile(name,text,mime){const url=URL.createObjectURL(new Blob([text],{type:mime}));const anchor=document.createElement("a");anchor.href=url;anchor.download=name;document.body.append(anchor);anchor.click();anchor.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
function downloadCSV(name,headers,data){const quote=value=>'"'+String(value??"").replaceAll('"','""')+'"';const text="\ufeff"+[headers,...data].map(row=>row.map(quote).join(";")).join("\r\n");downloadFile(name,text,"text/csv;charset=utf-8");}
function exportGeoJSON(){
    const ids=new Set(filteredRows().map(row=>row.block_id));
    const collection={
        type:"FeatureCollection",
        source:{blocks:"Кварталы по улицам, железным дорогам и воде OpenStreetMap",attributes:"Расчёт BlocksNet по данным OpenStreetMap",schools:"Геометрия школьных корпусов из учебной работы «Моделирование территории»; вместимость по сведениям сайтов образовательных организаций",network:"Дорожная сеть построена из сохранённых данных OpenStreetMap"},
        modes:{walk:"Пешком",drive:"На автомобиле"},
        units:{time:"min",area:"m2",coordinates:"degrees"},
        coordinate_reference_system:"EPSG:4326",
        features:DATA.blocks.features.filter(feature=>ids.has(feature.properties.block_id))
    };
    if(!collection.features.length)return;
    downloadFile("shlisselburg_blocks.geojson",JSON.stringify(collection,null,2),"application/geo+json;charset=utf-8");
}
function filteredRows(){const land=$("land-filter").value,morph=$("morph-filter").value;return rows.filter(row=>(!land||String(row.land_use)===land)&&(!morph||String(row.spacematrix_morphotype)===morph));}
function compare(a,b,direction){const aMissing=a===null||a===undefined,bMissing=b===null||b===undefined;if(aMissing||bMissing)return aMissing===bMissing?0:aMissing?1:-1;if(typeof a==="number"&&typeof b==="number")return(a-b)*direction;return String(a).localeCompare(String(b),"ru",{numeric:true})*direction;}
function fillSelectors(){const options=rows.map(row=>'<option value="'+row.block_id+'">Квартал '+row.block_id+'</option>').join("");$("matrix-origin").innerHTML=options;$("matrix-destination").innerHTML='<option value="">Все кварталы</option>'+options;for(const [id,key]of[["land-filter","land_use"],["morph-filter","spacematrix_morphotype"]]){const values=[...new Set(rows.map(row=>row[key]).filter(value=>value!==null&&value!==undefined))].sort((a,b)=>String(a).localeCompare(String(b),"ru"));$(id).insertAdjacentHTML("beforeend",values.map(value=>'<option value="'+esc(value)+'">'+esc(translate(value,key))+'</option>').join(""));}fillMapMetrics();}
function fillMapMetrics(){
    const previous=$("map-metric").value;
    const all=["land_use","population","school_count","capacity","fsi","spacematrix_morphotype","strelka_morphotype",mode+"_area_accessibility",mode+"_provision_strong",mode+"_provision_weak"];
    $("map-metric").innerHTML=all.map(key=>'<option value="'+key+'">'+esc(info(key,false).label)+'</option>').join("");
    const adapted=previous.replace(/^(walk|drive)_/,mode+"_");
    $("map-metric").value=all.includes(adapted)?adapted:"land_use";
}

function focusSelected(){if(!map||!hasSelection)return;blockLayer.eachLayer(layer=>{if(layer.feature.properties.block_id===selected)map.fitBounds(layer.getBounds(),{maxZoom:17,padding:[30,30],animate:false});});}
function clearSelected(){hasSelection=false;$("fit-selected").hidden=true;renderInspector();renderBlocks();if(blockLayer){blockLayer.setStyle(styleBlock);syncMapAccessibility();}}
function setSelected(id,zoom=false,allowOutside=false){if(!byId.has(Number(id)))return;const next=Number(id),visible=filteredRows().some(row=>row.block_id===next);if(!visible&&!allowOutside)return;hasSelection=visible;selected=next;$("fit-selected").hidden=!hasSelection;$("matrix-origin").value=String(selected);matrixPage=0;renderInspector();renderMatrix();if(blockLayer){blockLayer.setStyle(styleBlock);syncMapAccessibility();if(zoom)focusSelected();}renderBlocks();}
function renderSummary(){
    const items=filteredRows(),population=sumKnown(items,"population"),capacity=sumKnown(items,"capacity"),schools=sumKnown(items,"school_count");
    const schoolProperties=DATA.schools.features.map(feature=>feature.properties);
    const estimated=schoolProperties.filter(properties=>properties.capacity_estimated===true);
    const assumed=[...new Set(estimated.map(properties=>properties.capacity).filter(finite))];
    const note=estimated.length?(assumed.length===1?"Для корпусов без данных принято по "+fmt(assumed[0],0)+" мест. Это условие модели.":"Часть вместимости задана условно для корпусов без исходных данных."):"683 и 934 места по сведениям сайтов образовательных организаций; дата сведений не указана";
    const cards=[
        ["Кварталы",items.length,"Границы по улицам, железным дорогам и воде"],
        ["Расчётное население",population.value,"Оценка по жилой площади: 30 м² на человека"],
        ["Школьные корпуса",schools.value,"Корпуса одной школы учитываются отдельно"],
        [estimated.length===schoolProperties.length&&estimated.length?"Условная вместимость, мест":estimated.length?"Вместимость с оценками, мест":"Вместимость школ, мест",capacity.value,note]
    ];
    $("summary-cards").innerHTML=cards.map(([label,value,note])=>'<div class="card"><div class="label">'+esc(label)+'</div><div class="value">'+fmt(value,0)+'</div><div class="note">'+esc(note)+'</div></div>').join("");
    $("filter-count").textContent=items.length+" из "+rows.length+" кварталов";
    $("export-blocks").disabled=!items.length;$("export-geojson").disabled=!items.length;
}

function schoolTravelTimes(){
    const matrix=DATA.matrices[mode],origin=matrix.origins.indexOf(selected);
    return rows.filter(row=>finite(row.capacity)&&row.capacity>0).map(row=>{
        const target=matrix.destinations.indexOf(row.block_id);
        return {block_id:row.block_id,time:origin>=0&&target>=0?matrix.values[origin][target]:null};
    });
}
function schoolTimesHTML(){
    const times=schoolTravelTimes();
    return '<section class="school-times" aria-labelledby="school-times-title"><h4 id="school-times-title">Время до кварталов со школами · '+modeTitle[mode]+helpButton('school_times')+'</h4><dl class="school-time-list">'+times.map(item=>'<div class="school-time-row" data-school-block="'+item.block_id+'"><dt>До квартала '+item.block_id+' со школой</dt><dd>'+(finite(item.time)?fmt(item.time,2)+' мин':'Нет пути')+'</dd></div>').join('')+'</dl><p class="school-time-note">Расчёт между точками кварталов с учётом подходов к дорожной сети.</p></section>';
}
function renderInspector(){
    const row=byId.get(selected),mapKey=$("map-metric").value,mapMeta=info(mapKey,false);
    $("metric-description").innerHTML='<strong>'+esc(mapMeta.label)+'.</strong> '+esc(mapMeta.description);
    if(!hasSelection){
        $("inspector").innerHTML='<div class="inspector-top"><h3>Выбранный квартал</h3></div><div class="inspector-empty"><strong>Выберите квартал на карте</strong><p>Нажмите на квартал на карте или выберите строку в таблице ниже.</p></div>';
        return;
    }
    const key=mapMeta.kind==="category"?mode+"_provision_strong":mapKey,meta=info(key,false);
    const number=isUnreachable(row,key)?"∞ мин":rowDisplay(row,key,true);
    const isProvision=/^(walk|drive)_provision_(strong|weak)$/.test(key);
    const provisionNumbers=isProvision?'<p class="explanation">Расчётная потребность в школьных местах: '+esc(places(row[mode+"_demand"]))+'; выделено до 15 мин: '+esc(places(row[mode+"_demand_within"]))+'.</p>':"";
    const readings=[["Площадь",finite(row.site_area)?fmt(row.site_area/10000,2)+" га":"Не определено","site_area"],["Расчётное население",display(row.population,"population",true),"population"],["FSI",display(row.fsi,"fsi"),"fsi"],["GSI",display(row.gsi,"gsi"),"gsi"],["Средняя этажность",display(row.l,"l"),"l"],["Вместимость школ",display(row.capacity,"capacity",true),"capacity"]];
    let html='<div class="inspector-top"><h3>Квартал '+selected+'</h3><button type="button" id="clear-selection">Снять выбор</button></div><p class="intro">'+esc(translate(row.land_use,"land_use"))+helpButton('land_use')+'</p>'+(finite(row.share)?'<p class="land-share">Этой категорией размечено '+fmt(row.share*100,2)+'% площади квартала</p>':'')+'<p class="secondary">'+esc(morphotypeTitle(row.spacematrix_morphotype))+helpButton('spacematrix_morphotype')+'</p><dl class="basic-grid">'+readings.map(([label,value,field])=>'<div><dt><span class="metric-label">'+esc(label)+helpButton(field)+'</span></dt><dd>'+esc(value)+'</dd></div>').join("")+'</dl><div class="metric-highlight"><div class="caption">'+esc(meta.label)+' · '+modeTitle[mode]+helpButton(key)+'</div><div class="number">'+esc(number)+'</div>'+provisionNumbers+(isProvision?"":'<p class="explanation">'+esc(isUnreachable(row,key)?"∞: в сохранённой сети есть недостижимые кварталы. Причины указаны в выводах ниже; отдельные направления доступны в таблице времени.":meta.description)+'</p>')+'</div>'+schoolTimesHTML()+'<details><summary>Все показатели квартала</summary><div class="all-attributes">';
    for(const[group,name]of Object.entries(groupNames)){
        const keys=[...schema(),...extraKeys].filter(field=>field!=="block_id"&&info(field).group===group);
        if(!keys.length)continue;
        html+='<h4 class="attribute-group">'+name+'</h4>'+keys.map(field=>'<div class="detail-row"><span title="'+esc(info(field).description)+'">'+esc(info(field,false).label)+(info(field).kind==="category"?"":" ("+esc(info(field).unit)+")")+helpButton(field)+'</span><strong>'+cell(row,field)+'</strong></div>').join("");
    }
    $("inspector").innerHTML=html+'</div></details>';
    $("clear-selection").addEventListener("click",clearSelected);
}

function scaleFor(items,key){const meta=info(key);if(meta.kind==="category"){const values=[...new Set(items.map(row=>row[key]).filter(value=>value!==null&&value!==undefined))].sort((a,b)=>String(a).localeCompare(String(b),"ru",{numeric:true}));return{category:true,values,color:value=>categoryPalettes[key]?.[value]||MAP_STYLE.missing};}const values=items.map(row=>row[key]).filter(finite);const min=values.length?Math.min(...values):0,max=values.length?Math.max(...values):0;return{category:false,min,max,hasValues:values.length>0,color:value=>colors[max===min?2:Math.max(0,Math.min(4,Math.floor((value-min)/(max-min)*5)))]};}
function styleBlock(feature){
    const row=feature.properties,key=$("map-metric").value,shown=filteredIdSet.has(row.block_id),active=hasSelection&&row.block_id===selected;
    const value=row[key],missing=value===null||value===undefined;
    return{color:active?"__MAP_selection__":missing&&!isUnreachable(row,key)?MAP_STYLE.missingOutline:"__MAP_blockBoundary__",weight:active?3:.8,opacity:shown?1:.2,fillColor:isUnreachable(row,key)?"url(#unreachable-hatch)":missing?"__MAP_missing__":mapScale.color(value),fillOpacity:shown?1:.075,dashArray:missing&&!isUnreachable(row,key)?"3 3":null};
}
function installMapPatterns(){
    const svg=map.getPane("blocks-analysis").querySelector("svg");if(!svg)return;
    const ns="http://www.w3.org/2000/svg",defs=document.createElementNS(ns,"defs"),pattern=document.createElementNS(ns,"pattern");
    pattern.id="unreachable-hatch";pattern.setAttribute("patternUnits","userSpaceOnUse");pattern.setAttribute("width","8");pattern.setAttribute("height","8");
    const rect=document.createElementNS(ns,"rect");rect.setAttribute("width","8");rect.setAttribute("height","8");rect.setAttribute("fill",MAP_STYLE.unreachable);
    const lines=document.createElementNS(ns,"path");lines.setAttribute("d","M-2 2L2-2M0 8L8 0M6 10L10 6");lines.setAttribute("stroke",MAP_STYLE.hatchForeground);lines.setAttribute("stroke-width",".8");
    pattern.append(rect,lines);defs.append(pattern);svg.prepend(defs);
}
function syncMapAccessibility(){if(!blockLayer)return;blockLayer.eachLayer(layer=>{const element=layer.getElement(),row=layer.feature.properties,key=$("map-metric").value,shown=filteredIdSet.has(row.block_id);if(!element)return;element.setAttribute("tabindex",shown?"0":"-1");element.setAttribute("role","button");element.setAttribute("aria-pressed",String(hasSelection&&row.block_id===selected));element.setAttribute("aria-label","Квартал "+row.block_id+", "+translate(row.land_use,"land_use")+", "+info(key).label+": "+(isUnreachable(row,key)?"бесконечное среднее":rowDisplay(row,key,true)));});}

let filteredIdSet=new Set(rows.map(row=>row.block_id));
function renderLegend(){const key=$("map-metric").value,meta=info(key,false);let html='<div class="legend-title">'+esc(meta.label)+' ('+esc(meta.unit)+')</div>';if(mapScale.category)html+=mapScale.values.map(value=>'<div class="legend-item"><span class="swatch" style="background:'+mapScale.color(value)+'"></span>'+esc(translate(value,key))+'</div>').join("");else if(mapScale.hasValues){if(mapScale.min===mapScale.max)html+='<div class="legend-item"><span class="swatch" style="background:'+colors[2]+'"></span>'+esc(display(mapScale.min,key))+'</div>';else for(let i=0;i<5;i++){const from=mapScale.min+(mapScale.max-mapScale.min)*i/5,to=mapScale.min+(mapScale.max-mapScale.min)*(i+1)/5;html+='<div class="legend-item"><span class="swatch" style="background:'+colors[i]+'"></span>'+esc(display(from,key))+" … "+esc(display(to,key))+'</div>';}}const visible=filteredRows();if(visible.some(row=>isUnreachable(row,key)))html+='<div class="legend-item"><span class="swatch" style="background:repeating-linear-gradient(135deg,__MAP_hatchForeground__ 0,__MAP_hatchForeground__ 1px,__MAP_unreachable__ 1px,__MAP_unreachable__ 6px)"></span>∞: есть недостижимые назначения</div>';if(visible.some(row=>noDemandRatio(row,key)))html+='<div class="legend-item"><span class="swatch" style="background:__MAP_missing__;border:1px dashed __MAP_missingOutline__"></span>Не определяется: потребность 0</div>';if(visible.some(row=>(row[key]===null||row[key]===undefined)&&!isUnreachable(row,key)&&!noDemandRatio(row,key)))html+='<div class="legend-item"><span class="swatch" style="background:__MAP_missing__;border:1px dashed __MAP_missingOutline__"></span>'+(key.includes("morphotype")?"Не классифицировано":"Не определено")+'</div>';$("legend").innerHTML=html+'<div class="legend-note">Шкала всей территории</div>';}
function renderProvisionLinks(){
    const selectedOrigins=new Set(filteredRows().map(row=>row.block_id));
    const links=DATA.links[mode].features.map(feature=>feature.properties).filter(p=>selectedOrigins.has(Number(p.source))).sort((a,b)=>a.source-b.source||a.target-b.target);
    const matrix=DATA.matrices[mode];
    $("flow-note").textContent="Режим: "+modeTitle[mode].toLocaleLowerCase("ru")+". Связей в выборке: "+links.length+".";
    $("provision-links-table").querySelector("tbody").innerHTML=links.length?links.map(p=>{
        const i=matrix.origins.indexOf(Number(p.source)),j=matrix.destinations.indexOf(Number(p.target));
        const time=i>=0&&j>=0?matrix.values[i][j]:null;
        return '<tr><td class="id-col">'+esc(p.source)+'</td><td class="number">'+esc(p.target)+'</td><td class="number">'+fmt(p.value,0)+'</td><td class="number">'+(finite(time)?fmt(time,2):"Не определено")+"</td></tr>";
    }).join(""):'<tr><td colspan="4">В этой выборке межквартальных распределений нет.</td></tr>';
}
function fitCity(){
    if(map&&cityBounds?.isValid()){map.invalidateSize({pan:false,animate:false});map.fitBounds(cityBounds,{padding:[20,20],animate:false});}
}
function streetStyle(feature){
    const raw=feature.properties.highway;
    const highway=Array.isArray(raw)?raw[0]:raw;
    const major=["motorway","trunk","primary","secondary"].includes(highway);
    const medium=["tertiary","residential","living_street","unclassified"].includes(highway);
    return{color:major?"__MAP_roadMajor__":"__MAP_roadMinor__",weight:major?2.6:medium?1.5:1,opacity:.95,lineCap:"round",lineJoin:"round"};
}
function initMap(){
    if(typeof L==="undefined"){
        $("map").innerHTML='<div class="map-unavailable">Карта недоступна: Leaflet не загрузился. Проверьте подключение к интернету. Все сохранённые значения доступны в таблицах ниже.</div>';
        $("legend").hidden=true;
        return;
    }
    map=L.map("map",{preferCanvas:false,scrollWheelZoom:false,zoomSnap:.25,zoomDelta:.5});
    for(const[name,zIndex]of[["land-base",200],["water-base",300],["blocks-analysis",410],["buildings-base",420],["streets-base",430],["schools-layer",450],["territory-outline",460]]){
        map.createPane(name);
        map.getPane(name).style.zIndex=zIndex;
    }
    map.attributionControl.addAttribution('© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, ODbL');
    L.control.scale({imperial:false,position:"bottomright"}).addTo(map);
    landLayer=L.geoJSON(DATA.locality||DATA.boundary,{
        pane:"land-base",interactive:false,style:{stroke:false,fillColor:"__MAP_land__",fillOpacity:1}
    }).addTo(map);
    waterLayer=L.geoJSON(DATA.water,{
        pane:"water-base",interactive:false,style:{color:"__MAP_shore__",weight:.7,fillColor:"__MAP_water__",fillOpacity:1}
    }).addTo(map);
    blockLayer=L.geoJSON(DATA.blocks,{
        pane:"blocks-analysis",style:styleBlock,
        onEachFeature:(feature,layer)=>{
            layer.bindTooltip(()=>{
                const row=feature.properties,key=$("map-metric").value;
                return "<strong>Квартал "+row.block_id+"</strong><br>"+esc(translate(row.land_use,"land_use"))+"<br>"+esc(info(key).label)+": "+(isUnreachable(row,key)?"∞ мин":esc(rowDisplay(row,key,true)));
            });
            layer.on("click",()=>setSelected(feature.properties.block_id));
            layer.on("add",()=>{
                const element=layer.getElement();
                element.addEventListener("keydown",event=>{if(event.key==="Enter"||event.key===" "){event.preventDefault();event.stopPropagation();setSelected(feature.properties.block_id);}if(event.key==="Escape"){event.preventDefault();layer.closeTooltip();clearSelected();}});
                element.addEventListener("focus",()=>layer.openTooltip());
                element.addEventListener("blur",()=>layer.closeTooltip());
            });
        }
    }).addTo(map);
    syncMapAccessibility();
    roadsLayer=L.geoJSON(DATA.roads,{
        pane:"streets-base",filter:feature=>["LineString","MultiLineString"].includes(feature.geometry.type),
        style:streetStyle,
        onEachFeature:(feature,layer)=>{
            const raw=feature.properties.name;
            const name=Array.isArray(raw)?raw.join(", "):raw;
            layer.bindTooltip(esc(name||"Улица без названия в OSM"),{sticky:true});
        }
    }).addTo(map);
    boundaryLayer=L.geoJSON(DATA.boundary,{
        pane:"territory-outline",interactive:false,
        style:{color:"__MAP_boundary__",weight:1.6,fill:false,dashArray:"6 5"}
    }).addTo(map);
    schoolLayer=L.geoJSON(DATA.schools,{
        pane:"schools-layer",
        pointToLayer:(_feature,latlng)=>L.marker(latlng,{pane:"schools-layer",icon:L.divIcon({className:"school-marker",iconSize:[13,13]})}),
        style:{color:"__MAP_school__",weight:2,fillOpacity:.5},
        onEachFeature:(feature,layer)=>{
            const p=feature.properties;
            layer.bindPopup("<strong>"+esc(p.name||"Школьный корпус")+"</strong><br>Вместимость: "+places(p.capacity)+"<br>"+
                (p.capacity_estimated?"В исходных данных вместимость не указана. Для модели принято "+places(p.capacity)+".":"Источник вместимости: сайты образовательных организаций"));
        }
    }).addTo(map);
    if(DATA.buildings){
        buildingLayer=L.geoJSON(DATA.buildings,{
            pane:"buildings-base",interactive:false,
            style:{color:"__MAP_building__",weight:.5,fillColor:"__MAP_building__",fillOpacity:.22}
        });
    }else $("buildings-control").hidden=true;
    renderProvisionLinks();
    cityBounds=blockLayer.getBounds();
    fitCity();
    installMapPatterns();
    syncMapAccessibility();
}

function renderMap(){const visible=filteredRows();filteredIdSet=new Set(visible.map(row=>row.block_id));mapScale=scaleFor(rows,$("map-metric").value);if(blockLayer){blockLayer.setStyle(styleBlock);syncMapAccessibility();}renderLegend();$("map-count").textContent="Улицы и вода: OpenStreetMap";}
function groupColumns(){
    const group=$("column-group").value;
    if(group==="both")return[...baseKeys,...["walk","drive"].flatMap(modeKeys)];
    if(group==="all")return schema();
    if(group==="overview")return["block_id","land_use","population","fsi","site_area",mode+"_capacity",mode+"_provision_strong"];
    if(group==="provision"&&!$("provision-all").checked)return["block_id",mode+"_demand",mode+"_demand_within",mode+"_demand_left",mode+"_provision_strong"];
    return["block_id",...schema().filter(key=>key!=="block_id"&&info(key).group===group)];
}
const expectedKeys=new Set([...baseKeys,...["walk","drive"].flatMap(m=>modeFields.map(key=>m+"_"+key))]);
const extraKeys=[...new Set(rows.flatMap(row=>Object.keys(row)))].filter(key=>!expectedKeys.has(key)&&!/(^|_)reachable($|_)/.test(key));


function renderBlocks(){closeHelp();const isProvision=$("column-group").value==="provision";$("provision-options").hidden=!isProvision;$("provision-summary").hidden=!isProvision;const filtered=filteredRows(),columns=groupColumns(),size=$("blocks-size").value==="all"?Math.max(1,filtered.length):Number($("blocks-size").value);if(!columns.includes(blockSort.key))blockSort=$("column-group").value==="provision"?{key:mode+"_demand",direction:-1}:{key:"block_id",direction:1};const positiveDemand=filtered.filter(row=>row[mode+"_demand"]>0).length;$("provision-summary").innerHTML=esc("Расчёт охватывает все "+rows.length+" кварталов; в текущей выборке потребность в местах больше нуля в "+positiveDemand+" из "+filtered.length+".")+helpButton(mode+"_demand");const sorted=[...filtered].sort((a,b)=>compare(a[blockSort.key],b[blockSort.key],blockSort.direction));const pages=Math.max(1,Math.ceil(sorted.length/size));blockPage=Math.min(blockPage,pages-1);const pageRows=sorted.slice(blockPage*size,(blockPage+1)*size);$("blocks-table").querySelector("thead").innerHTML="<tr>"+columns.map((key,i)=>'<th scope="col" class="'+(i===0?"id-col":info(key).kind!=="category"?"number":"category")+'" aria-sort="'+(blockSort.key===key?(blockSort.direction===1?"ascending":"descending"):"none")+'">'+headerMarkup(esc(info(key).label)+(blockSort.key===key?(blockSort.direction===1?" ↑":" ↓"):"")+'<span class="unit">'+esc(info(key).unit)+'</span>',key==="block_id"?null:key,'data-key="'+esc(key)+'" aria-label="Сортировать: '+esc(info(key).label)+'"')+'</th>').join("")+"</tr>";$("blocks-table").querySelector("tbody").innerHTML=pageRows.length?pageRows.map(row=>'<tr data-id="'+row.block_id+'" tabindex="0" aria-label="Выбрать квартал '+row.block_id+'" class="'+(hasSelection&&row.block_id===selected?"selected":"")+'">'+columns.map((key,i)=>'<td class="'+(i===0?"id-col":info(key).kind!=="category"?"number":"")+'" title="'+esc(rowDisplay(row,key,true))+'">'+cell(row,key)+'</td>').join("")+'</tr>').join(""):'<tr><td colspan="'+columns.length+'" class="empty">Нет кварталов по выбранным условиям.</td></tr>';$("table-count").textContent=filtered.length+" из "+rows.length+" кварталов";$("blocks-page-status").textContent=filtered.length?(blockPage*size+1)+" … "+Math.min((blockPage+1)*size,filtered.length)+" из "+filtered.length:"Нет строк";$("blocks-page").textContent=(blockPage+1)+" / "+pages;$("blocks-prev").disabled=blockPage===0;$("blocks-next").disabled=blockPage>=pages-1;}
function matrixRows(){const matrix=DATA.matrices[mode],index=matrix.origins.indexOf(selected),values=matrix.values[index];return matrix.destinations.map((destination,i)=>({destination,time:values[i],population:byId.get(destination).population,capacity:byId.get(destination).capacity}));}
function renderMatrix(){const full=matrixRows(),destination=$("matrix-destination").value,filter=$("matrix-filter").value;const items=full.filter(row=>(destination===""||String(row.destination)===destination)&&(filter==="all"||filter==="reachable"&&row.time!==null||filter==="within"&&row.time!==null&&row.time<=15||filter==="unreachable"&&row.time===null)).sort((a,b)=>compare(a[matrixSort.key],b[matrixSort.key],matrixSort.direction));const pages=Math.max(1,Math.ceil(items.length/20));matrixPage=Math.min(matrixPage,pages-1);$("matrix-table").querySelector("tbody").innerHTML=items.slice(matrixPage*20,(matrixPage+1)*20).map(row=>'<tr data-id="'+row.destination+'" class="'+(row.destination===selected?"selected":"")+'"><td class="id-col">Квартал '+row.destination+'</td><td class="number">'+(row.time===null?'<span class="unreachable">Недостижимо</span>':fmt(row.time))+'</td><td class="number">'+fmt(row.population,0)+'</td><td class="number">'+fmt(row.capacity,0)+'</td></tr>').join("")||'<tr><td colspan="4" class="empty">Нет назначений по выбранным условиям.</td></tr>';const reachable=full.filter(row=>row.time!==null).length,within=full.filter(row=>row.time!==null&&row.time<=15).length;$("matrix-stats").innerHTML=[["Достижимо",reachable+" / "+full.length],["До 15 минут",within],["Недостижимо",full.length-reachable]].map(([label,value])=>'<span><strong>'+esc(value)+'</strong>'+label+'</span>').join("");$("matrix-page-status").textContent="Из квартала "+selected+". "+(items.length?(matrixPage*20+1)+" … "+Math.min((matrixPage+1)*20,items.length)+" из "+items.length:"Нет строк");$("matrix-page").textContent=(matrixPage+1)+" / "+pages;$("matrix-prev").disabled=matrixPage===0;$("matrix-next").disabled=matrixPage>=pages-1;$("matrix-table").querySelectorAll("th button[data-sort]").forEach(button=>{button.setAttribute("aria-label",button.textContent.trim()+", сортировать");button.closest("th").setAttribute("aria-sort",button.dataset.sort===matrixSort.key?(matrixSort.direction===1?"ascending":"descending"):"none");});}
const chartLayout={paper_bgcolor:"transparent",plot_bgcolor:"transparent",font:{family:"Segoe UI, Arial, sans-serif",color:MAP_STYLE.ui.muted,size:11},margin:{l:47,r:15,t:14,b:45},xaxis:{gridcolor:MAP_STYLE.ui.grid,zerolinecolor:MAP_STYLE.ui.grid,automargin:true},yaxis:{gridcolor:MAP_STYLE.ui.grid,zerolinecolor:MAP_STYLE.ui.grid,automargin:true},showlegend:false};
const chartConfig={responsive:true,displayModeBar:false,locale:"ru"};
function renderCharts(){
    const items=filteredRows(),provision=items.filter(row=>finite(row[mode+"_provision_strong"]));
    $("chart-provision-note").textContent="Доля покрытия определена для "+provision.length+" из "+items.length+" кварталов. При нулевой потребности доля не считается: в формуле получается деление на 0.";
    if(typeof Plotly==="undefined"){for(const id of["chart-provision","chart-density"])$(id).innerHTML='<div class="empty">Для графика требуется загрузка Plotly из интернета.</div>';return;}
    Plotly.react("chart-provision",[{type:"histogram",x:provision.map(row=>row[mode+"_provision_strong"]*100),marker:{color:MAP_STYLE.charts.histogram,line:{color:MAP_STYLE.charts.histogram_outline,width:.8}},hovertemplate:"Обеспеченность: %{x}%<br>Кварталов: %{y}<extra></extra>"}],{...chartLayout,xaxis:{...chartLayout.xaxis,title:"Покрытие, %"},yaxis:{...chartLayout.yaxis,title:"Кварталов",nticks:6}},chartConfig);
    const density=items.filter(row=>finite(row.fsi)&&finite(row.gsi)),known=density.filter(row=>finite(row[mode+"_provision_strong"])),unknown=density.filter(row=>!finite(row[mode+"_provision_strong"]));
    const trace=(data,marker)=>({type:"scatter",mode:"markers",x:data.map(row=>row.gsi*100),y:data.map(row=>row.fsi),customdata:data.map(row=>row.block_id),text:data.map(row=>"Квартал "+row.block_id+"<br>Обеспеченность: "+rowDisplay(row,mode+"_provision_strong")),marker,hovertemplate:"%{text}<br>GSI: %{x:.1f}%<br>FSI: %{y:.2f}<extra></extra>"});
    Plotly.react("chart-density",[
        trace(known,{size:9,opacity:1,color:known.map(row=>row[mode+"_provision_strong"]),colorscale:colors.map((color,i)=>[i/(colors.length-1),color]),cmin:0,cmax:1,showscale:known.length>0,colorbar:{title:"Покрытие, %",tickformat:".0%",len:.8,thickness:10},line:{width:.8,color:MAP_STYLE.charts.point_outline}}),
        trace(unknown,{size:9,opacity:1,symbol:MAP_STYLE.charts.missing_symbol,color:MAP_STYLE.charts.missing_color,line:{width:1,color:MAP_STYLE.charts.missing_outline}})
    ],{...chartLayout,margin:{...chartLayout.margin,r:77},xaxis:{...chartLayout.xaxis,title:"GSI, %"},yaxis:{...chartLayout.yaxis,title:"FSI, м²/м²"}},chartConfig);
    $("chart-density").removeAllListeners?.("plotly_click");$("chart-density").on("plotly_click",event=>{const id=event.points?.[0]?.customdata;if(id!==undefined)setSelected(id,true);});
}

function nestedValue(value){if(value===null||value===undefined)return"Не определено";if(typeof value==="object")return'<code>'+esc(JSON.stringify(value,null,2))+'</code>';return esc(typeof value==="boolean"?(value?"Да":"Нет"):value);}
function manualDisplay(value,unit){
    if(!finite(value))return nestedValue(value);
    const countUnits=new Set(["записей","мест","объектов","полигонов","учреждений","чел."]);
    const digits=unit==="км²"?3:countUnits.has(unit)?0:2;
    const rounded=Number(value.toFixed(digits));
    return esc((rounded===0?0:rounded).toLocaleString("ru-RU",{maximumFractionDigits:digits}));
}

const metadataLabels={title:"Название",city:"Город",captured_at:"Получение XML-выборки OSM завершено",crs:"Система координат расчёта",source:"Источник",sources:"Источники",school_parameters:"Параметры школьного сценария",boundary_scope:"Область границы",boundary_locality:"Граница населённого пункта",source_roads:"Исходные дороги",updated_at:"Обновлено",status:"Состояние",area_km2:"Площадь, км²",osm_type:"Тип объекта OSM",osm_id:"Идентификатор OSM",display_name:"Название в OSM",bounds:"Границы координат",count:"Количество",tags:"Теги OSM",clipped_to_boundary:"Обрезано по границе",block_count:"Количество кварталов",blocks:"Кварталы",population:"Население",schools:"Объекты школ",school_count:"Количество объектов школ",total_capacity:"Вместимость школ",capacity:"Вместимость",walk:"Пешком",drive:"На автомобиле",boundary:"Граница территории",buildings:"Здания",land_use:"Использование территории по OSM",density:"Показатели плотности",spacematrix:"Spacematrix",strelka:"Strelka",accessibility:"Доступность",provision:"Обеспеченность"};
function keyValueTable(id,object){$(id).innerHTML=Object.entries(object).map(([key,value])=>'<tr><th scope="row">'+esc(metadataLabels[key]||key)+'</th><td>'+nestedValue(value)+'</td></tr>').join("")||'<tr><td>В исходном файле нет записей.</td></tr>';}
function listHTML(items,empty){return items.length?items.map(item=>'<li>'+esc(item)+'</li>').join(""):'<li>'+esc(empty)+'</li>';}
function renderDocumentation(){
    document.title="Шлиссельбург | Кварталы и доступность";
    $("page-title").textContent="Шлиссельбург";
    const captured=new Date(DATA.manifest.captured_at);$("footer-date").textContent="XML-выборка OSM получена: "+(Number.isFinite(captured.getTime())?captured.toLocaleDateString("ru-RU",{day:"numeric",month:"long",year:"numeric"}):DATA.manifest.captured_at);
    const keys=["cluster","l","fsi","mxi","spacematrix_morphotype","morphotype"].filter(key=>DATA.clusters.some(row=>Object.hasOwn(row,key)));
    const names={cluster:"Кластер",l:"L, этажей",fsi:"FSI, м²/м²",mxi:"MXI, %",spacematrix_morphotype:"Расчётный морфотип",morphotype:"Расчётный морфотип"};
    $("clusters-table").innerHTML='<thead><tr>'+keys.map(key=>'<th class="'+(['l','fsi','mxi','cluster'].includes(key)?'number':'category')+'">'+headerMarkup(esc(names[key]),key==='cluster'?'spacematrix_cluster':key==='morphotype'?'spacematrix_morphotype':key)+'</th>').join("")+'</tr></thead><tbody>'+DATA.clusters.map(row=>'<tr>'+keys.map(key=>'<td class="'+(['l','fsi','mxi','cluster'].includes(key)?'number':'')+'">'+(key==="mxi"?esc(display(row[key],"mxi")):key.includes("morphotype")?esc(morphotypeTitle(row[key])):fmt(row[key]))+'</td>').join("")+'</tr>').join("")+'</tbody>';
}

function renderConclusions(){
    const population=sumKnown(rows,"population").value,demand=sumKnown(rows,"walk_demand").value,capacity=sumKnown(rows,"capacity").value;
    const paragraphs=["В модели "+rows.length+" кварталов по улицам и водоёмам. Оценка населения по жилой площади: "+fmt(population,0)+" человек."];
    if(finite(demand)&&demand>0){const walk=sumKnown(rows,"walk_demand_within").value,drive=sumKnown(rows,"drive_demand_within").value;paragraphs.push("Вместимость школ: "+places(capacity)+", расчётная потребность: "+places(demand)+". В пределах 15 минут покрыто "+fmt(walk/demand*100,1)+"% потребности при пешем передвижении и "+fmt(drive/demand*100,1)+"% при поездке на автомобиле.");}
    if(rows.some(row=>["walk","drive"].some(m=>row[m+"_area_accessibility_status"]==="unreachable")))paragraphs.push("Среднее время до всех кварталов равно ∞ из-за отдельных недостижимых направлений. В пешей сети с основной частью города не связаны кварталы 90, 91, 95 и 96; в автомобильной нет допустимого подключения кварталов с 90 по 96. Квартал 96 соответствует Орешку, водный транспорт не учтён. Остальные случаи связаны со структурой загруженной сети OSM и проверкой подходов через воду; физическая изоляция этих участков не установлена.");
    $("conclusions-paragraphs").innerHTML=paragraphs.map(text=>'<p style="margin-bottom:10px">'+esc(text)+'</p>').join("");
}
function refreshFilters(){renderProvisionLinks();blockPage=0;const visible=filteredRows();if(hasSelection&&!visible.some(row=>row.block_id===selected))clearSelected();renderSummary();renderBlocks();renderMap();renderCharts();}
function switchMode(next){if(mode===next)return;mode=next;document.querySelectorAll("[data-mode]").forEach(button=>button.setAttribute("aria-pressed",String(button.dataset.mode===mode)));$("mode-note").textContent=mode==="walk"?"Пешая модель, скорость 5 км/ч":"Без пробок и поиска парковки; подходы пешком 5 км/ч";$("active-mode-label").textContent=mode==="walk"?"Пешая доступность":"Автомобильная доступность";blockSort.key=blockSort.key.replace(/^(walk|drive)_/,mode+"_");fillMapMetrics();renderProvisionLinks();renderInspector();renderBlocks();renderMap();renderMatrix();renderCharts();}
function bindEvents(){document.querySelectorAll("[data-mode]").forEach(button=>button.addEventListener("click",()=>switchMode(button.dataset.mode)));$("map-metric").addEventListener("change",()=>{renderMap();renderInspector();});$("fit-selected").addEventListener("click",focusSelected);$("matrix-origin").addEventListener("change",event=>setSelected(event.target.value,false,true));$("fit-city").addEventListener("click",fitCity);$("fit-map").addEventListener("click",()=>{if(map)map.fitBounds(boundaryLayer.getBounds().isValid()?boundaryLayer.getBounds():blockLayer.getBounds(),{padding:[20,20],animate:false});});for(const[id,layer]of[["layer-schools",()=>schoolLayer],["layer-buildings",()=>buildingLayer]])$(id).addEventListener("change",event=>{if(!map||!layer())return;if(event.target.checked)layer().addTo(map);else map.removeLayer(layer());});for(const id of["land-filter","morph-filter"])$(id).addEventListener("change",refreshFilters);$("reset-filters").addEventListener("click",()=>{$("land-filter").value="";$("morph-filter").value="";refreshFilters();});$("column-group").addEventListener("change",()=>{blockPage=0;if($("column-group").value==="provision")blockSort={key:mode+"_demand",direction:-1};renderBlocks();});$("provision-all").addEventListener("change",()=>{blockPage=0;renderBlocks();});$("blocks-size").addEventListener("change",()=>{blockPage=0;renderBlocks();});$("blocks-table").addEventListener("keydown",event=>{if((event.key==="Enter"||event.key===" ")&&!event.target.closest("button")){const row=event.target.closest("tr[data-id]");if(row){event.preventDefault();setSelected(row.dataset.id,true);}}});$("blocks-table").addEventListener("click",event=>{const button=event.target.closest("button[data-key]");if(button){blockSort={key:button.dataset.key,direction:blockSort.key===button.dataset.key?-blockSort.direction:1};renderBlocks();return;}const row=event.target.closest("tr[data-id]");if(row)setSelected(row.dataset.id,true);});for(const[id,change]of[["blocks-prev",-1],["blocks-next",1]])$(id).addEventListener("click",()=>{blockPage+=change;renderBlocks();});for(const id of["matrix-destination","matrix-filter"])$(id).addEventListener("change",()=>{matrixPage=0;renderMatrix();});$("matrix-table").addEventListener("click",event=>{const button=event.target.closest("button[data-sort]");if(button){matrixSort={key:button.dataset.sort,direction:matrixSort.key===button.dataset.sort?-matrixSort.direction:1};renderMatrix();return;}});for(const[id,change]of[["matrix-prev",-1],["matrix-next",1]])$(id).addEventListener("click",()=>{matrixPage+=change;renderMatrix();});$("export-blocks").addEventListener("click",()=>{const items=filteredRows();if(!items.length)return;const keys=[...new Set(DATA.blocks.features.flatMap(feature=>Object.keys(feature.properties)))];downloadCSV("shlisselburg_blocks.csv",keys,items.map(row=>keys.map(key=>isUnreachable(row,key)?"inf":row[key])));});$("export-geojson").addEventListener("click",exportGeoJSON);$("export-origin").addEventListener("click",()=>downloadCSV("shlisselburg_"+mode+"_from_"+selected+".csv",["source","target","time_min"],matrixRows().map(row=>[selected,row.destination,row.time===null?"inf":row.time])));$("export-matrix").addEventListener("click",()=>{const matrix=DATA.matrices[mode];downloadCSV("shlisselburg_"+mode+"_matrix.csv",["block_id",...matrix.destinations],matrix.origins.map((origin,i)=>[origin,...matrix.values[i].map(value=>value===null?"inf":value)]));});}
try{fillSelectors();renderProvisionLinks();renderSummary();renderDocumentation();renderConclusions();renderMap();renderBlocks();renderMatrix();renderInspector();bindEvents();bindHelp();initMap();renderCharts();}catch(error){$("startup-error").hidden=false;$("startup-error").textContent="Часть страницы не удалось отобразить: "+error.message;console.error(error);}
</script>
</body>
</html>
'''


def main() -> None:
    parser = argparse.ArgumentParser(description="Собрать локальную страницу Шлиссельбурга из сохранённых результатов.")
    parser.add_argument("--output", type=Path, default=HERE / "dashboard.html", help="Путь HTML, по умолчанию рядом со скриптом")
    parser.add_argument("--check", action="store_true", help="Проверить входные результаты без записи HTML")
    args = parser.parse_args()
    try:
        payload = load_payload()
        if args.check:
            print(f"Проверка пройдена: {len(payload['blocks']['features'])} кварталов, два режима и матрицы прочитаны.")
            return
        serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        serialized = serialized.replace("&", "\\u0026").replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
        map_style = read_json(HERE / "map_style.json")
        html = HTML
        for role, color in map_style.items():
            if isinstance(color, str):
                html = html.replace("__MAP_" + role + "__", color)
        for role, color in map_style.get("ui", {}).items():
            html = html.replace("__UI_" + role + "__", color)
        html = html.replace("__MAP_STYLE__", json.dumps(map_style, ensure_ascii=False, allow_nan=False))
        html = html.replace("__DASHBOARD_DATA__", serialized)
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(html, encoding="utf-8", newline="\n")
        temporary.replace(output)
        print(f"Сохранено: {output}")
        print("Все данные и векторная основа встроены в HTML. Интернет нужен для загрузки Leaflet, библиотеки карты, и Plotly, библиотеки графиков.")
    except (OSError, ValueError, TypeError) as error:
        parser.exit(1, f"Сборка остановлена: {error}\n")


if __name__ == "__main__":
    main()
