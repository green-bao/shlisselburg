"""Локальное приложение для сравнения вариантов размещения школ."""

from __future__ import annotations

import argparse
import copy
import csv
import io
import json
import mimetypes
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import engine

ROOT = Path(__file__).resolve().parent
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def _json_file(name):
    path = ROOT / "data" / name
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def bootstrap():
    return {
        "geometry": engine.geometry_data(),
        "water": _json_file("water.geojson"),
        "roads": _json_file("roads.geojson"),
        "schools": _json_file("schools.geojson"),
        "baselines": {mode: engine.baseline(mode) for mode in ("walk", "drive")},
        "metadata": engine.metadata(),
    }


def comparison_csv(mode, additions):
    result = engine.calculate(mode, additions)
    original = {b["block_id"]: b for b in engine.baseline(mode)["blocks"]}
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";")
    writer.writerow([
        "Режим", "Квартал", "Население, чел.", "Потребность, мест",
        "Вместимость исходная, мест", "Вместимость сценария, мест",
        "Обеспечено исходно, мест", "Обеспечено в сценарии, мест",
        "Обеспеченность исходная, %", "Обеспеченность сценария, %",
        "Дефицит исходный, мест", "Дефицит сценария, мест", "Сокращение дефицита, мест",
    ])
    for row in result["blocks"]:
        base = original[row["block_id"]]
        percentage = lambda value: "" if value is None else round(value * 100, 6)
        writer.writerow([
            "Пешком" if mode == "walk" else "На автомобиле", row["block_id"],
            row["population"], row["demand"], base["capacity"], row["capacity"],
            base["supplied"], row["supplied"], percentage(base["provision"]),
            percentage(row["provision"]), base["deficit"], row["deficit"],
            base["deficit"] - row["deficit"],
        ])
    return output.getvalue().encode("utf-8-sig")


def _search_worker(job_id, mode, capacity):
    try:
        from search import search

        def update(snapshot):
            with JOBS_LOCK:
                JOBS[job_id].update(copy.deepcopy(snapshot))

        result = search(mode, capacity, progress_callback=update)
        with JOBS_LOCK:
            JOBS[job_id].update(copy.deepcopy(result))
            JOBS[job_id]["status"] = "complete"
    except Exception as exc:
        with JOBS_LOCK:
            JOBS[job_id].update(status="failed", error=str(exc))


class Handler(BaseHTTPRequestHandler):
    def _send(self, payload, status=200, content_type="application/json; charset=utf-8", filename=None):
        if isinstance(payload, bytes):
            content = payload
        else:
            content = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/bootstrap":
                return self._send(bootstrap())
            if path.startswith("/api/search/"):
                with JOBS_LOCK:
                    job = copy.deepcopy(JOBS.get(path.rsplit("/", 1)[-1]))
                return self._send(job or {"error": "Поиск не найден."}, 200 if job else 404)
            filename = "index.html" if path == "/" else path.lstrip("/")
            target = (ROOT / "static" / filename).resolve()
            if not target.is_relative_to(ROOT / "static") or not target.is_file():
                return self._send({"error": "Страница не найдена."}, 404)
            mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            return self._send(target.read_bytes(), content_type=mime + "; charset=utf-8")
        except Exception as exc:
            self._send({"error": str(exc)}, 500)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                raise ValueError("Недопустимый размер запроса.")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("Запрос должен содержать объект с параметрами.")
            mode = body.get("mode")
            if path == "/api/calculate":
                return self._send(engine.calculate(mode, body.get("additions", [])))
            if path == "/api/export-csv":
                return self._send(comparison_csv(mode, body.get("additions", [])),
                                  content_type="text/csv; charset=utf-8", filename=f"school-placement-{mode}.csv")
            if path == "/api/search":
                capacity = body.get("capacity")
                if type(capacity) is not int or capacity <= 0:
                    raise ValueError("Вместимость должна быть положительным целым числом.")
                total = len(engine.baseline(mode)["blocks"])
                job_id = uuid.uuid4().hex
                with JOBS_LOCK:
                    if any(job["status"] == "running" for job in JOBS.values()):
                        raise ValueError("Дождитесь завершения текущего автоматического поиска.")
                    JOBS[job_id] = {"job_id": job_id, "status": "running", "mode": mode,
                                    "capacity": capacity, "total": total, "checked": 0,
                                    "ranking": [], "errors": [], "best_block_ids": [], "complete": False}
                threading.Thread(target=_search_worker, args=(job_id, mode, capacity), daemon=True).start()
                return self._send({"job_id": job_id}, 202)
            return self._send({"error": "Действие не найдено."}, 404)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            self._send({"error": str(exc)}, 400)
        except Exception as exc:
            self._send({"error": str(exc)}, 500)

    def log_message(self, fmt, *args):
        if args and str(args[1:2]) != "('200',)":
            super().log_message(fmt, *args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Приложение открывается по адресу http://127.0.0.1:{args.port}", flush=True)
    print("Для завершения нажмите Ctrl+C в этом окне.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
