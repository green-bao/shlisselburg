"""Compare saved factual roads, water and buildings with the local OSM extract.

Run with the project Python environment::

    python compare_layers.py --reference "path/to/Финал"

Only comparison.html is written by default. All source GeoJSON files stay intact.
Use --evidence to save the measured values separately when needed.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import html
import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
from pyproj import Transformer
import shapely


HERE = Path(__file__).resolve().parent
METRIC_CRS = 32636
TO_METRIC = Transformer.from_crs(4326, METRIC_CRS, always_xy=True).transform
POLYGONS = {"Polygon", "MultiPolygon"}
LINES = {"LineString", "MultiLineString"}


def metric(geom):
    return shapely.transform(geom, TO_METRIC, interleaved=False)


def select(gdf, kinds):
    return gdf.loc[gdf.geom_type.isin(kinds)].copy()


def union(gdf):
    return shapely.union_all(shapely.make_valid(gdf.geometry.to_numpy()))


def measure(geom, kind="area"):
    projected = metric(geom)
    return float(projected.area if kind == "area" else projected.length)


def inspect(path):
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    frame = gpd.read_file(path).to_crs(4326)
    keys = [shapely.to_wkb(shapely.normalize(g)) for g in frame.geometry]
    counts = Counter(keys)
    rows = [json.dumps(x, ensure_ascii=False, sort_keys=True) for x in raw["features"]]
    info = {
        "filename": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "rows": len(frame),
        "types": frame.geom_type.value_counts().to_dict(),
        "invalid": int((~frame.is_valid).sum()),
        "empty": int(frame.is_empty.sum()),
        "full_row_duplicate_extras": len(rows) - len(set(rows)),
        "same_geometry_extra_rows": len(keys) - len(counts),
        "same_geometry_groups": sum(n > 1 for n in counts.values()),
    }
    if "osm_key" in frame:
        value_counts = frame.osm_key.dropna().value_counts()
        info.update(osm_key_nonnull=int(frame.osm_key.notna().sum()),
                    osm_key_unique=int(frame.osm_key.nunique()),
                    osm_key_duplicate_extras=int((value_counts - 1).sum()))
    else:
        info["osm_key_nonnull"] = None
        info["osm_key_unique"] = None
        info["osm_key_duplicate_extras"] = None
    return frame, info


def cover(manual, osm, boundary, kind):
    if kind == "area":
        manual = manual.to_crs(METRIC_CRS)
        osm = osm.to_crs(METRIC_CRS)
    m = union(manual).intersection(boundary)
    o = union(osm).intersection(boundary)
    shared = m.intersection(o)
    only_m = m.difference(o)
    only_o = o.difference(m)
    denominator = m.union(o)
    value = (lambda g: float(g.area)) if kind == "area" else (lambda g: measure(g, "length"))
    numbers = {"manual": value(m), "osm": value(o), "shared": value(shared),
               "only_manual": value(only_m), "only_osm": value(only_o), "combined": value(denominator)}
    numbers["shared_percent_manual"] = 100 * numbers["shared"] / numbers["manual"] if numbers["manual"] else None
    numbers["shared_percent_osm"] = 100 * numbers["shared"] / numbers["osm"] if numbers["osm"] else None
    drawing = (lambda g: g) if kind == "area" else metric
    vectors = {"manual": drawing(m), "osm": drawing(o), "shared": drawing(shared),
               "only_manual": drawing(only_m), "only_osm": drawing(only_o)}
    return numbers, vectors


def exact_matches(manual, osm):
    """No radius or coordinate rounding; topological equality of full contours."""
    mg = shapely.make_valid(manual.geometry.to_numpy())
    og = shapely.make_valid(osm.geometry.to_numpy())
    tree = shapely.STRtree(og)
    matched_m, matched_o, pairs = set(), set(), []
    intersecting_m, intersecting_o = set(), set()
    for i, g in enumerate(mg):
        for k in tree.query(g, predicate="intersects"):
            k = int(k)
            if g.intersection(og[k]).area > 0:
                intersecting_m.add(i)
                intersecting_o.add(k)
            if shapely.equals(g, og[k]):
                matched_m.add(i)
                matched_o.add(k)
                pairs.append({"manual_fid": int(manual.iloc[i].fid), "osm_key": str(osm.iloc[k].osm_key)})
    return {
        "exact_manual": len(matched_m), "exact_osm": len(matched_o),
        "positive_overlap_not_exact_manual": len(intersecting_m - matched_m),
        "positive_overlap_not_exact_osm": len(intersecting_o - matched_o),
        "no_positive_overlap_manual": len(mg) - len(intersecting_m),
        "no_positive_overlap_osm": len(og) - len(intersecting_o), "exact_pairs": pairs,
    }


def within_overlap(frame):
    frame = frame.to_crs(METRIC_CRS)
    geoms = shapely.make_valid(frame.geometry.to_numpy())
    tree = shapely.STRtree(geoms)
    pairs = []
    for i, geom in enumerate(geoms):
        for k in tree.query(geom, predicate="intersects"):
            k = int(k)
            if k <= i:
                continue
            overlap = geom.intersection(geoms[k])
            if overlap.area > 0:
                a = str(frame.iloc[i].get("osm_key", frame.iloc[i].get("fid", i)))
                b = str(frame.iloc[k].get("osm_key", frame.iloc[k].get("fid", k)))
                pairs.append({"a": a, "b": b, "area_m2": float(overlap.area),
                              "equal_geometry": bool(geom.equals(geoms[k]))})
    return sorted(pairs, key=lambda p: p["area_m2"], reverse=True)


def fmt(value, places=0):
    if value is None:
        return "не задано"
    return f"{value:,.{places}f}".replace(",", "\u202f").replace(".", ",")


def table(headers, rows, classes=""):
    head = "".join(f"<th>{x}</th>" for x in headers)
    body = "".join("<tr>" + "".join(f"<td>{x}</td>" for x in row) + "</tr>" for row in rows)
    return f'<div class="table-wrap"><table class="{classes}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def as_map(geometry):
    # No simplification or buffer; round only the drawing coordinates to 1 mm.
    def rounded(value):
        if isinstance(value, (list, tuple)):
            return [rounded(x) for x in value]
        return round(float(value), 3)
    data = shapely.geometry.mapping(geometry)
    if "coordinates" in data:
        return {"type": data["type"], "coordinates": rounded(data["coordinates"])}
    return {"type": data["type"], "geometries": [as_map(x) for x in geometry.geoms]}


def build(reference, output, evidence=None):
    source_dir = reference / "Геоданные" if (reference / "Геоданные").is_dir() else reference
    datasets, sources = {}, {}
    for side, root, names in [
        ("manual", source_dir, {"boundary": "boundary.geojson", "buildings": "building.geojson", "roads": "roads.geojson", "water": "water.geojson", "lake": "lake.geojson", "river": "river.geojson"}),
        ("osm", HERE / "data/raw", {"boundary": "boundary.geojson", "buildings": "buildings.geojson", "roads": "roads.geojson", "water": "water.geojson"}),
    ]:
        for layer, name in names.items():
            path = root / name
            if not path.is_file():
                if layer in {"boundary", "buildings"} or side == "osm":
                    raise FileNotFoundError(path)
                continue
            key = f"{side}_{layer}"
            datasets[key], sources[key] = inspect(path)

    boundary_m = union(datasets["manual_boundary"].to_crs(METRIC_CRS))
    boundary_o = union(datasets["osm_boundary"].to_crs(METRIC_CRS))
    boundary = boundary_m.intersection(boundary_o)
    boundary_geographic = union(datasets["manual_boundary"]).intersection(union(datasets["osm_boundary"]))
    result = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "measurement_crs": f"EPSG:{METRIC_CRS}",
        "overlay_crs": {"area": f"EPSG:{METRIC_CRS}", "line_coincidence_and_contour_equality": "EPSG:4326; exact set operations, no snapping or buffer"},
        "sources": sources,
        "boundary": {"shared_m2": float(boundary.area), "manual_only_m2": float(boundary_m.difference(boundary_o).area),
                     "osm_only_m2": float(boundary_o.difference(boundary_m).area)},
        "layers": {},
    }
    snapshot_path = HERE / "data/raw/osm_snapshot.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8")) if snapshot_path.is_file() else {}
    result["osm_snapshot"] = {k: snapshot.get(k) for k in ["source", "source_url", "started_at", "finished_at", "sha256", "limitations"]}
    if snapshot_path.is_file():
        result["osm_snapshot"]["metadata_file_sha256"] = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    map_data = {"boundary": as_map(boundary), "layers": {}}
    for name, types, kind in [("buildings", POLYGONS, "area"), ("roads", LINES, "length"), ("water", POLYGONS, "area")]:
        if f"manual_{name}" not in datasets:
            result["layers"][name] = {"missing_manual_file": f"{name}.geojson"}
            continue
        manual = select(datasets[f"manual_{name}"], types)
        osm = select(datasets[f"osm_{name}"], types)
        coverage, vectors = cover(manual, osm, boundary if kind == "area" else boundary_geographic, kind)
        coverage.update(manual_count=len(manual), osm_count=len(osm),
                        manual_full_sum=sum(measure(g, kind) for g in manual.geometry),
                        osm_full_sum=sum(measure(g, kind) for g in osm.geometry),
                        manual_full_union=float(union(manual.to_crs(METRIC_CRS)).area) if kind == "area" else measure(union(manual), kind),
                        osm_full_union=float(union(osm.to_crs(METRIC_CRS)).area) if kind == "area" else measure(union(osm), kind))
        if kind == "area":
            coverage["manual_overlap_pairs"] = within_overlap(manual)
            coverage["osm_overlap_pairs"] = within_overlap(osm)
        if name == "buildings":
            coverage.update(exact_matches(manual, osm))
            coverage["footprint_attribute_sum"] = float(pd.to_numeric(manual.footprint_area).sum())
            coverage["footprint_common_attribute_values"] = {str(k): int(v) for k, v in manual.footprint_area.value_counts().head(3).items()}
            coverage["full_property_duplicate_excluding_fid_extras"] = int(manual.drop(columns=["fid", "geometry"]).duplicated().sum())
        if name == "roads":
            numeric = pd.to_numeric(manual.get("id"), errors="coerce")
            candidates = manual.loc[numeric.notna() & (numeric > 0) & (numeric % 1 == 0)].copy()
            candidates["candidate_osm_key"] = numeric.loc[candidates.index].astype("int64").map(lambda n: f"way/{n}")
            joined = candidates.merge(osm[["osm_key", "geometry", "highway"]], left_on="candidate_osm_key", right_on="osm_key", suffixes=("_manual", "_osm"))
            coverage["positive_numeric_manual_ids"] = len(candidates)
            coverage["numeric_ids_matching_osm_way"] = len(joined)
            coverage["numeric_id_pairs_equal_geometry"] = sum(bool(shapely.equals(a, b)) for a, b in zip(joined.geometry_manual, joined.geometry_osm))
            coverage["manual_id_missing"] = int(numeric.isna().sum())
            coverage["manual_id_zero"] = int((numeric == 0).sum())
            coverage["highway_counts_manual"] = manual.highway.value_counts().to_dict()
            coverage["highway_counts_osm"] = osm.highway.value_counts().to_dict()
        result["layers"][name] = coverage
        bounds = vectors["manual"].union(vectors["osm"]).bounds
        map_data["layers"][name] = {k: as_map(v) for k, v in vectors.items()} | {"bounds": bounds}
    if all(f"manual_{x}" in datasets for x in ["water", "lake", "river"]):
        lr = union(datasets["manual_lake"].to_crs(METRIC_CRS)).union(union(datasets["manual_river"].to_crs(METRIC_CRS)))
        w = union(datasets["manual_water"].to_crs(METRIC_CRS))
        result["water_auxiliary_check"] = {"lake_river_outside_water_m2": float(lr.difference(w).area), "water_outside_lake_river_m2": float(w.difference(lr).area)}

    b = result["layers"]["buildings"]
    roads = result["layers"].get("roads", {})
    water = result["layers"].get("water", {})
    coverage_rows = []
    for name, title, kind in [("buildings", "Здания, га", "area"), ("roads", "Дороги, км", "length"), ("water", "Вода, га", "area")]:
        row = result["layers"][name]
        if "missing_manual_file" in row:
            coverage_rows.append([title, "нет сохранённого слоя", "", "", "", ""])
            continue
        divisor = 10000 if kind == "area" else 1000
        coverage_rows.append([title] + [fmt(row[key] / divisor, 2) for key in ["manual", "osm", "shared", "only_manual", "only_osm"]])
    coverage_table = table(["Слой и единицы", "Комплект", "OSM", "Общая часть", "Только комплект", "Только OSM"], coverage_rows)
    matching_table = table(["Полные контуры зданий", "Комплект", "OSM"], [
        ["Точно равные контуры", fmt(b["exact_manual"]), fmt(b["exact_osm"])],
        ["Есть пересечение по площади, контуры не равны", fmt(b["positive_overlap_not_exact_manual"]), fmt(b["positive_overlap_not_exact_osm"])],
        ["Нет пересечения по площади с другим набором", fmt(b["no_positive_overlap_manual"]), fmt(b["no_positive_overlap_osm"])],
    ])
    quality_rows = []
    for layer, title in [("buildings", "Здания"), ("roads", "Дороги"), ("water", "Вода")]:
        for side, label in [("manual", "комплект"), ("osm", "OSM")]:
            info = sources.get(f"{side}_{layer}")
            if not info:
                quality_rows.append([f"{title}, {label}", "слой не найден", "", "", "", ""])
                continue
            ids = "нет OSM-ID" if info["osm_key_nonnull"] is None else str(info["osm_key_duplicate_extras"])
            types = "; ".join(f"{k}: {v}" for k, v in info["types"].items())
            quality_rows.append([f"{title}, {label}", f'{fmt(info["rows"])}<small>{html.escape(types)}</small>',
                                 str(info["full_row_duplicate_extras"]), str(info["same_geometry_extra_rows"]), ids, str(info["invalid"])])
    quality_table = table(["Набор", "Строки и геометрия", "Полные дубли строк", "Повторы контура", "Повторы OSM-ID", "Некорректные геометрии"], quality_rows)
    areas_table = table(["Здания, м²", "Комплект", "OSM"], [
        ["Сумма площадей контуров", fmt(b["manual_full_sum"], 2), fmt(b["osm_full_sum"], 2)],
        ["Площадь объединения контуров", fmt(b["manual_full_union"], 2), fmt(b["osm_full_union"], 2)],
        ["Повторно учтённая площадь при простом суммировании", fmt(max(0, b["manual_full_sum"] - b["manual_full_union"]), 2), fmt(max(0, b["osm_full_sum"] - b["osm_full_union"]), 2)],
        ["Сумма поля footprint_area", fmt(b["footprint_attribute_sum"], 2), "поле отсутствует"],
    ])
    road_text = '<p>В комплекте не найден сохранённый roads.geojson. Сопоставление дорог не выполнено.</p>'
    road_table = ""
    if "manual_count" in roads:
        road_text = f'''<p>В комплекте {fmt(roads["manual_count"])} линий, в OSM {fmt(roads["osm_count"])} линейные записи. Число записей зависит от разбиения сети. Поэтому сравнивается также длина объединённой геометрии. Общая точная линия составляет {fmt(roads["shared"]/1000,2)} км. Сдвинутые и упрощённые линии могут описывать ту же дорогу и при этом не попасть в точное совпадение.</p>
        <p>В ручном поле <code>id</code> {fmt(roads["positive_numeric_manual_ids"])} положительных целых значений; {fmt(roads["numeric_ids_matching_osm_way"])} совпали с номерами линейных объектов OSM типа <code>way</code>, в том числе {fmt(roads["numeric_id_pairs_equal_geometry"])} с равной геометрией. Тип объекта в ручном файле не задан. В {fmt(roads["manual_id_missing"])} строках поле <code>id</code> не заполнено, ещё в одной указано значение 0. Эти числовые совпадения показаны отдельно и не используются как подтверждённая история правок.</p>'''
        classes = sorted(set(roads["highway_counts_manual"]) | set(roads["highway_counts_osm"]))
        road_table = table(["Класс highway", "Линии в комплекте", "Линейные записи OSM"], [[f"<code>{html.escape(k)}</code>", fmt(roads["highway_counts_manual"].get(k, 0)), fmt(roads["highway_counts_osm"].get(k, 0))] for k in classes])
    water_text = '<p>В комплекте не найден сохранённый water.geojson. Сопоставление воды не выполнено.</p>'
    if "manual_count" in water:
        equal_pairs = [p for p in water["osm_overlap_pairs"] if p["equal_geometry"]]
        pair_text = "; ".join(f'<code>{html.escape(p["a"])}</code> и <code>{html.escape(p["b"])}</code>' for p in equal_pairs)
        water_text = f'''<p>Сравниваются {water["manual_count"]} полигональная запись <code>water.geojson</code> комплекта и {water["osm_count"]} полигональные записи OSM. Ещё {sum(v for k,v in sources["osm_water"]["types"].items() if k in LINES)} линейных водных объектов и {sources["osm_water"]["types"].get("Point",0)} точка в OSM исключены из площади.</p>
        <p>Сумма площадей водных полигонов OSM равна {fmt(water["osm_full_sum"]/1e6,3)} км², площадь их объединения {fmt(water["osm_full_union"]/1e6,3)} км². В сохранённой выгрузке {pair_text} имеют одинаковую геометрию. Это составные объекты OSM типа <code>relation</code>, собранные из частей. Объединение учитывает эту площадь один раз; равенство относится к геометриям файлов в рассматриваемой области.</p>'''
        if "water_auxiliary_check" in result:
            extra = result["water_auxiliary_check"]
            water_text += f'<p>Геометрии из <code>lake.geojson</code> и <code>river.geojson</code> полностью входят в <code>water.geojson</code>. В <code>water.geojson</code> есть ещё {fmt(extra["water_outside_lake_river_m2"],0)} м² водной площади. Эти два вспомогательных файла не прибавляются к воде повторно.</p>'
    source_rows = []
    for key, info in sources.items():
        side, layer = key.split("_", 1)
        label = "Комплект" if side == "manual" else "OSM"
        source_rows.append([label, html.escape(info["filename"]), f'<code class="hash">{info["sha256"]}</code>'])
    for label, path in [("Комплект", source_dir / "boundary.qmd"), ("OSM", snapshot_path)]:
        if path.is_file():
            checksum = hashlib.sha256(path.read_bytes()).hexdigest()
            source_rows.append([label, html.escape(path.name), f'<code class="hash">{checksum}</code>'])
    source_table = table(["Источник", "Файл", "SHA-256 сохранённой редакции"], source_rows)
    snapshot_date = snapshot.get("finished_at", "дата не записана")
    if snapshot.get("finished_at"):
        snapshot_date = datetime.fromisoformat(snapshot["finished_at"]).strftime("%d.%m.%Y, %H:%M:%S UTC%z")
    ratio = b["footprint_attribute_sum"] / b["manual_full_sum"]
    document = TEMPLATE.replace("__DATA__", json.dumps(map_data, ensure_ascii=False, separators=(",", ":")))
    replacements = {
        "__MANUAL_BUILDINGS__": fmt(b["manual_count"]), "__OSM_BUILDINGS__": fmt(b["osm_count"]),
        "__EXACT_BUILDINGS__": fmt(b["exact_manual"]), "__BUILDING_DIFFERENCE__": fmt(b["only_manual"] / 10000, 2),
        "__BOUNDARY__": fmt(result["boundary"]["shared_m2"] / 1e6, 3),
        "__BOUNDARY_M__": fmt(result["boundary"]["manual_only_m2"] / 10000, 2),
        "__BOUNDARY_O__": fmt(result["boundary"]["osm_only_m2"] / 10000, 2),
        "__COVERAGE__": coverage_table, "__MATCHING__": matching_table, "__AREAS__": areas_table,
        "__QUALITY__": quality_table, "__ROAD_TEXT__": road_text, "__ROAD_TABLE__": road_table,
        "__WATER_TEXT__": water_text, "__SOURCES__": source_table, "__OSM_DATE__": html.escape(snapshot_date),
        "__FOOTPRINT_RATIO__": fmt(ratio, 2), "__MANUAL_INVALID__": str(sources["manual_buildings"]["invalid"]),
        "__SOURCE_DATE__": datetime.now().strftime("%d.%m.%Y"),
    }
    for key, value in replacements.items():
        document = document.replace(key, value)
    output.write_text(document, encoding="utf-8")
    if evidence:
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {output.name}: {output.stat().st_size:,} bytes")
    print(json.dumps({"buildings": {k:v for k,v in b.items() if not isinstance(v,(list,dict))}, "boundary": result["boundary"]}, ensure_ascii=False, indent=2))


TEMPLATE = r'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Шлиссельбург: сравнение исходных слоёв</title>
<style>
:root{--paper:#f6f7f5;--ink:#1f302f;--muted:#61706c;--line:#dce3dc;--manual:#18796b;--osm:#d77930;--shared:#414b67}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}main{max-width:1180px;margin:auto;padding:46px 32px 60px}h1{font-size:34px;line-height:1.15;font-weight:650;letter-spacing:-.8px;margin:6px 0 18px}h2{font-size:23px;line-height:1.25;margin:0 0 14px;letter-spacing:-.3px}p{margin:10px 0 16px}a{color:#166b60}code{font-size:.88em}header{max-width:920px}.eyebrow{text-transform:uppercase;letter-spacing:1.7px;font-size:12px;color:var(--muted);font-weight:650}.lead{font-size:19px;line-height:1.5;max-width:850px}.notice{border-left:3px solid var(--manual);padding:2px 0 2px 17px;max-width:940px}.notice p{margin:8px 0}.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:15px;margin:30px 0}.stat{padding:18px 22px;border:1px solid var(--line);border-radius:10px;background:white}.stat strong{display:block;font-size:30px;line-height:1.2;margin:6px 0}.stat small{color:var(--muted);font-size:14px}section{margin:38px 0}.muted{color:var(--muted)}.map-card{border:1px solid #d5ded6;background:#fff;border-radius:12px;overflow:hidden}.map-toolbar{display:flex;gap:14px;align-items:center;justify-content:space-between;padding:14px 16px;border-bottom:1px solid var(--line);flex-wrap:wrap}.tabs,.modes{display:flex;gap:5px;flex-wrap:wrap}button{font:inherit;font-size:14px;border:1px solid var(--line);border-radius:6px;background:white;color:var(--ink);padding:7px 13px;cursor:pointer}button:hover{background:#edf4ef}button[aria-pressed=true]{background:#214c43;color:#fff;border-color:#214c43}.modes button{font-size:13px;padding:6px 10px}.map-frame{height:505px;position:relative;background:#eef2ee}canvas{display:block;width:100%;height:100%;touch-action:none;cursor:grab}canvas:active{cursor:grabbing}.zoom{display:flex;position:absolute;right:12px;top:12px;gap:5px}.zoom button{background:#fff;font-size:17px;min-width:35px;padding:5px 9px}.map-caption{display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;padding:11px 16px;font-size:13px;color:var(--muted)}.legend{display:flex;flex-wrap:wrap;gap:16px}.legend span:before{content:"";display:inline-block;width:11px;height:11px;margin-right:6px;border-radius:2px;background:var(--manual)}.legend .osm:before{background:var(--osm)}.legend .shared:before{background:var(--shared)}.map-help{font-size:13px;padding:0 16px 12px;color:var(--muted)}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:8px;margin:16px 0;background:white}table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:right;padding:12px 15px;border-bottom:1px solid #e6ebe6;vertical-align:top}th{background:#edf1ec;font-weight:600;color:#40544c;line-height:1.35}th:first-child,td:first-child{text-align:left}tr:last-child td{border-bottom:0}td small{display:block;color:var(--muted);font-size:12px;margin-top:3px;max-width:250px}details{margin:20px 0}summary{cursor:pointer;font-weight:600}details p{max-width:1000px}.hash{font-size:10px;word-break:break-all}.method-list{padding-left:22px}.method-list li{padding:3px 0}.two-col{display:grid;grid-template-columns:1fr 1fr;gap:24px}.note{font-size:14px;color:var(--muted)}footer{font-size:13px;color:var(--muted);border-top:1px solid var(--line);padding-top:20px;margin-top:40px}@media(max-width:720px){main{padding:26px 18px 40px}h1{font-size:29px}.lead{font-size:17px}.stats{grid-template-columns:1fr;gap:9px}.stat{padding:14px 18px}.stat strong{font-size:25px}.two-col{grid-template-columns:1fr}.map-frame{height:430px}th,td{padding:10px;font-size:13px}.map-toolbar{gap:10px}.map-caption{font-size:12px}}@media print{body{background:#fff}main{max-width:none;padding:15px}.map-frame{height:400px}.map-toolbar,.zoom,.map-help{display:none}section{break-inside:avoid}.stats{margin:18px 0}details{display:block}}
</style></head><body><main>
<header><div class="eyebrow">Шлиссельбург · исходные геоданные</div><h1>Здания, дороги и вода:<br>сохранённый комплект и OSM</h1>
<p class="lead">Число контуров зданий в ручном комплекте: __MANUAL_BUILDINGS__, в сохранённой выгрузке OSM: __OSM_BUILDINGS__. Из них __EXACT_BUILDINGS__ контуров совпадают точно. Сравнение ниже показывает состав и геометрию двух наборов.</p></header>
<div class="notice"><p>По этим файлам нельзя установить, какие здания добавлялись в OSM вручную: в ручном слое нет OSM-ID, идентификаторов объектов в OpenStreetMap. Поэтому история публикации правок здесь не проверена. Различия наборов сами по себе не показывают, как изменился город.</p></div>
<div class="stats"><div class="stat"><small>Контуры зданий</small><strong>__MANUAL_BUILDINGS__ / __OSM_BUILDINGS__</strong><small>комплект / OSM; точки не включены</small></div><div class="stat"><small>Точное совпадение полных контуров</small><strong>__EXACT_BUILDINGS__</strong><small>без радиуса поиска и округления координат</small></div><div class="stat"><small>Застройка только в комплекте</small><strong>__BUILDING_DIFFERENCE__ га</strong><small>площадь геометрической разности в общей границе</small></div></div>
<section aria-label="Карта сравнения"><div class="map-card"><div class="map-toolbar"><div class="tabs" aria-label="Выбор слоя"><button data-layer="buildings" aria-pressed="true">Здания</button><button data-layer="roads" aria-pressed="false">Дороги</button><button data-layer="water" aria-pressed="false">Вода</button></div><div class="modes" aria-label="Режим карты"><button data-mode="difference" aria-pressed="true">Различия</button><button data-mode="manual" aria-pressed="false">Комплект</button><button data-mode="osm" aria-pressed="false">OSM</button></div></div><div class="map-frame"><canvas id="map" role="img" aria-label="Сравнение контуров зданий. Зелёным показана геометрия только в комплекте, оранжевым только в OSM, тёмным общая часть."></canvas><div class="zoom"><button id="zoom-in" aria-label="Приблизить">+</button><button id="zoom-out" aria-label="Отдалить">−</button><button id="fit" aria-label="Показать слой целиком">Весь слой</button></div></div><div class="map-caption"><div class="legend"><span id="manual-legend">Только комплект</span><span id="osm-legend" class="osm">Только OSM</span><span id="shared-legend" class="shared">Общая часть</span></div><span id="scale"></span></div><div class="map-help">Колесо мыши меняет масштаб; перетаскивание перемещает карту. Карта работает без интернета, фоновые тайлы не загружаются.</div></div></section>
<section><h2>Геометрическое покрытие в общей границе</h2><p>Единая область сравнения составляет __BOUNDARY__ км²: это пересечение двух сохранённых границ. За её пределами остаётся __BOUNDARY_M__ га ручной границы и __BOUNDARY_O__ га границы OSM. Площади и длины ниже получены после объединения геометрий внутри каждого набора.</p>__COVERAGE__<p class="note">Общая часть входит и в комплект, и в OSM. «Только комплект» и «Только OSM» обозначают геометрическую разность; это площадь или длина, а не число отсутствующих физических объектов.</p></section>
<section><h2>Здания: равные контуры и различия площади</h2><p>Сопоставлены полные полигональные геометрии файлов. Один контур может пересекаться с несколькими контурами другого набора, поэтому пространственное пересечение не принято за соответствие отдельных зданий.</p>__MATCHING__
<p>В OSM дополнительно присутствуют 4 точки с признаком здания. Они сохранены в исходном файле, но не имеют площади контура и не участвуют в этом сравнении.</p>__AREAS__
<p>Сумма ручного <code>footprint_area</code> в __FOOTPRINT_RATIO__ раза больше измеренной суммы контуров. Значение 195,97 повторяется у 1007 разных контуров, 197,67 у 532. Совпадение атрибутов не является дублем геометрии. Причина расхождения не установлена; атрибут не использован вместо измеренной площади.</p></section>
<section><h2>Дороги: геометрия и классы</h2>__ROAD_TEXT__<details><summary>Состав линейных записей по highway</summary>__ROAD_TABLE__<p class="note">В OSM дополнительно есть 30 точек. Они исключены из числа линий и длины. Число записей по классам не равно числу улиц.</p></details></section>
<section><h2>Вода: площадь без повторного счёта</h2>__WATER_TEXT__</section>
<section><h2>Дубли и корректность исходных геометрий</h2>__QUALITY__<p class="note">Обозначения типов: <code>Point</code> для точки, <code>LineString</code> для линии, <code>Polygon</code> для полигона. <code>MultiLineString</code> и <code>MultiPolygon</code> хранят набор линий или полигонов соответственно.</p><p class="note">Полный дубль означает равенство всей исходной записи. Повтор контура определяется по нормализованной геометрии, включая тип и координаты. У зданий дополнительно проверены топологически равные контуры: они покрывают одну и ту же область, даже если один записан как <code>Polygon</code>, а другой как <code>MultiPolygon</code>. Число повторов показывает лишние записи сверх первого экземпляра. __MANUAL_INVALID__ некорректные ручные геометрии зданий исправлены только в памяти перед пересечением. Исходные файлы не изменены. Внутренние перекрытия зданий комплекта по площади не обнаружены; в выгрузке OSM сумма площадей контуров превышает площадь их объединения на 71,35 м².</p></section>
<section><h2>Как выполнено сравнение</h2><ol class="method-list"><li>Взяты базовые <code>building.geojson</code>, <code>roads.geojson</code>, <code>water.geojson</code> сохранённого комплекта и соответствующие файлы <code>data/raw</code> OSM. Сценарные слои и кварталы не сравниваются.</li><li>Полигоны, линии и точки разделены по типу. Для площади используются полигоны, для длины дорог линии. Площади и общая граница сопоставлены в EPSG:32636, где координаты заданы в метрах.</li><li>Точное равенство полных контуров и совпадение дорожных линий проверены в исходных координатах WGS 84 без допуска по расстоянию. Для дорог объединение, пересечение с общей границей и геометрические разности выполнены до проекции, чтобы разбиение исходной линии не влияло на её точное совпадение; затем измерена длина в EPSG:32636. Геометрии со сдвигом не объявляются совпавшими автоматически.</li><li>Сумма площадей отдельных контуров, площадь их объединения и сумма ручного атрибута <code>footprint_area</code> посчитаны отдельно. Карта отображает вычисленные векторы; округление координат до миллиметра применяется только при записи карты и не участвует в измерениях.</li></ol>
<p>OSM сравнивается в сохранённой редакции выгрузки от __OSM_DATE__ по метаданным <code>osm_snapshot.json</code>. Дата состояния ручных слоёв в GeoJSON не указана; у сопутствующего <code>boundary.qmd</code> раздел дат пустой. Файловые даты не приняты за дату состояния территории.</p>
<p class="note">В метаданных OSM указано, что Map API может не вернуть пересекающий область объект без узла внутри запроса, а части крупных составных объектов типа <code>relation</code> могут быть неполными. Страница сравнивает сохранённые слои; полнота картографического описания города отдельно не установлена.</p>
<details><summary>Файлы и проверяемые редакции</summary>__SOURCES__<p class="note">Контрольная сумма SHA-256 позволяет проверить, что сравнивается та же сохранённая редакция файла.</p></details></section>
<footer>Сравнение подготовлено __SOURCE_DATE__. Геоданные OSM: © <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">участники OpenStreetMap</a>, ODbL. Сохранённый комплект: материалы учебного проекта по моделированию территории.</footer>
</main><script id="map-data" type="application/json">__DATA__</script>
<script>
'use strict';
const data=JSON.parse(document.getElementById('map-data').textContent), canvas=document.getElementById('map'), ctx=canvas.getContext('2d');
const colors={manual:'#18796b',osm:'#d77930',shared:'#414b67'};
let layer='buildings',mode='difference',scale=1,cx=0,cy=0,width=1,height=1,drag=null;
const names={buildings:'зданий',roads:'дорог',water:'воды'};
function ring(points){points.forEach((p,i)=>{const x=(p[0]-cx)*scale+width/2,y=height/2-(p[1]-cy)*scale;i?ctx.lineTo(x,y):ctx.moveTo(x,y)});}
function paint(g,color,alpha=1,lineOnly=false){if(!g)return;if(g.type==='GeometryCollection'){g.geometries.forEach(x=>paint(x,color,alpha,lineOnly));return}ctx.beginPath();let polygon=false;if(g.type==='Polygon'){g.coordinates.forEach(r=>{ring(r);ctx.closePath()});polygon=true}else if(g.type==='MultiPolygon'){g.coordinates.forEach(p=>p.forEach(r=>{ring(r);ctx.closePath()}));polygon=true}else if(g.type==='LineString'){ring(g.coordinates)}else if(g.type==='MultiLineString'){g.coordinates.forEach(ring)}else if(g.type==='Point'){return}ctx.globalAlpha=alpha;ctx.fillStyle=color;ctx.strokeStyle=color;ctx.lineWidth=layer==='roads'?1.65:.6;if(polygon&&!lineOnly){ctx.fill('evenodd')}if(!polygon||lineOnly)ctx.stroke();ctx.globalAlpha=1;}
function draw(){ctx.setTransform(devicePixelRatio,0,0,devicePixelRatio,0,0);ctx.clearRect(0,0,width,height);paint(data.boundary,'#e1e7df');paint(data.boundary,'#c3cec2',1,true);const d=data.layers[layer];if(!d)return;if(mode==='difference'){paint(d.only_manual,colors.manual,.88);paint(d.only_osm,colors.osm,.88);paint(d.shared,colors.shared,.82)}else paint(d[mode],colors[mode],.9);const meters=100/scale;const label=meters>=1000?`${(meters/1000).toLocaleString('ru-RU',{maximumFractionDigits:1})} км`:`${Math.round(meters).toLocaleString('ru-RU')} м`;document.getElementById('scale').textContent=`100 пикселей ≈ ${label}`;}
function fit(){const d=data.layers[layer];if(!d)return;let b=d.bounds;cx=(b[0]+b[2])/2;cy=(b[1]+b[3])/2;scale=Math.min((width-60)/(b[2]-b[0]||1),(height-60)/(b[3]-b[1]||1));draw();}
function resize(){const r=canvas.getBoundingClientRect();width=r.width;height=r.height;canvas.width=Math.round(width*devicePixelRatio);canvas.height=Math.round(height*devicePixelRatio);fit();}
function legend(){document.getElementById('manual-legend').textContent=mode==='difference'?'Только комплект':'Комплект';document.getElementById('osm-legend').textContent=mode==='difference'?'Только OSM':'OSM';document.getElementById('manual-legend').hidden=mode==='osm';document.getElementById('osm-legend').hidden=mode==='manual';document.getElementById('shared-legend').hidden=mode!=='difference';canvas.setAttribute('aria-label',`Сравнение ${names[layer]}. Режим: ${mode==='difference'?'геометрические различия':mode==='manual'?'сохранённый комплект':'OSM'}.`)}
document.querySelectorAll('[data-layer]').forEach(b=>{if(!data.layers[b.dataset.layer]){b.disabled=true;return}b.addEventListener('click',()=>{layer=b.dataset.layer;document.querySelectorAll('[data-layer]').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));legend();fit()})});document.querySelectorAll('[data-mode]').forEach(b=>b.addEventListener('click',()=>{mode=b.dataset.mode;document.querySelectorAll('[data-mode]').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));legend();draw()}));
document.getElementById('zoom-in').onclick=()=>{scale*=1.45;draw()};document.getElementById('zoom-out').onclick=()=>{scale/=1.45;draw()};document.getElementById('fit').onclick=fit;
canvas.addEventListener('wheel',e=>{e.preventDefault();const r=canvas.getBoundingClientRect(),x=e.clientX-r.left-width/2,y=e.clientY-r.top-height/2;const old=scale;scale*=Math.exp(-e.deltaY*.001);scale=Math.min(20,Math.max(.0001,scale));cx+=x/old-x/scale;cy-=y/old-y/scale;draw()},{passive:false});canvas.addEventListener('pointerdown',e=>{drag={x:e.clientX,y:e.clientY,cx,cy};canvas.setPointerCapture(e.pointerId)});canvas.addEventListener('pointermove',e=>{if(!drag)return;cx=drag.cx-(e.clientX-drag.x)/scale;cy=drag.cy+(e.clientY-drag.y)/scale;draw()});canvas.addEventListener('pointerup',e=>{drag=null;if(canvas.hasPointerCapture(e.pointerId))canvas.releasePointerCapture(e.pointerId)});canvas.addEventListener('pointercancel',()=>{drag=null});new ResizeObserver(resize).observe(canvas);legend();
</script></body></html>'''


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True, help="Directory containing factual Геоданные")
    parser.add_argument("--output", type=Path, default=HERE / "comparison.html")
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    build(args.reference, args.output, args.evidence)
