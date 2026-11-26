import argparse
import json
import logging
import os
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from service import DATA, ROOT, Reports, export_folder, image_file, prepare, read_json, stats
from state import encode

STATIC = Path(__file__).resolve().parent / "static"
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8")}
EXPORTS = {"report.html": "text/html; charset=utf-8", "videos.csv": "text/csv; charset=utf-8",
           "report.md": "text/markdown; charset=utf-8",
           "videos.xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
LOG = logging.getLogger("dashboard")


class Handler(BaseHTTPRequestHandler):
    def allowed(self, mutation=False):
        port = self.server.server_port
        hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
        if self.headers.get("Host") not in hosts:
            return False
        origin = self.headers.get("Origin")
        return not mutation or (not origin or origin in {"http://" + h for h in hosts})

    def send(self, data, content_type, status=HTTPStatus.OK, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        try:
            self.wfile.write(data)
        except ConnectionError:
            pass

    def json(self, value, status=HTTPStatus.OK):
        self.send(encode(value).encode(), "application/json; charset=utf-8", status)

    def do_GET(self):
        if not self.allowed():
            self.json({"error": "Приложение доступно только на localhost."}, HTTPStatus.FORBIDDEN)
            return
        path = urlsplit(self.path).path
        if path in ASSETS:
            name, mime = ASSETS[path]
            self.send((STATIC / name).read_bytes(), mime)
            return
        if path.startswith("/previews/"):
            image = image_file(path)
            if image and image.is_file():
                self.send(image.read_bytes(), "image/jpeg")
                return
        parts = path.strip("/").split("/")
        store = self.server.store
        if parts == ["api", "reports"]:
            self.json({"reports": store.list()})
            return
        if parts[:2] == ["api", "reports"] and len(parts) in (3, 4):
            report = store.get(parts[2])
            if report:
                folder = export_folder(report)
                files = [key for key in EXPORTS if (folder / key).is_file()]
                if len(parts) == 3:
                    self.json({**report, "stats": stats(report), "files": files,
                               "folder": str(folder) if files else None})
                    return
                file = parts[3]
                if file in files:
                    try:
                        data = (folder / file).read_bytes()
                    except FileNotFoundError:
                        pass
                    else:
                        self.send(data, EXPORTS[file], filename=file if file != "report.html" else None)
                        return
        self.json({"error": "Страница не найдена."}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        if not self.allowed(mutation=True):
            self.json({"error": "Запрос разрешён только из локального интерфейса."}, HTTPStatus.FORBIDDEN)
            return
        parts = urlsplit(self.path).path.strip("/").split("/")
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 262144 or self.headers.get_content_type() != "application/json":
                raise ValueError("Ожидается небольшой запрос JSON.")
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError("Некорректный запрос.")
            store = self.server.store
            if parts == ["api", "reports"]:
                if not isinstance(body.get("url"), str):
                    raise ValueError("Вставьте ссылку на профиль TikTok.")
                links = body.get("links", "")
                if not isinstance(links, str):
                    raise ValueError("Введите ссылки текстом, по одной на строку.")
                self.json(store.start(body["url"], body.get("limit"), body.get("refresh") is True, links,
                                      all_posts=body.get("all_posts") is True),
                          HTTPStatus.ACCEPTED)
                return
            if len(parts) == 4 and parts[:2] == ["api", "reports"]:
                if parts[3] == "delete":
                    if store.delete(parts[2]):
                        self.json({"ok": True})
                    else:
                        self.json({"error": "Отчёт не найден."}, HTTPStatus.NOT_FOUND)
                    return
                if parts[3] == "resume":
                    self.json(store.resume(parts[2]), HTTPStatus.ACCEPTED)
                    return
                if parts[3] == "folder":
                    report = store.get(parts[2])
                    if not report or not export_folder(report).is_dir():
                        raise ValueError("Файлы ещё не сохранены.")
                    os.startfile(export_folder(report))
                    self.json({"ok": True})
                    return
            self.json({"error": "Команда не найдена."}, HTTPStatus.NOT_FOUND)
        except (UnicodeError, ValueError) as exc:
            self.json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except RuntimeError as exc:
            self.json({"error": str(exc)}, HTTPStatus.CONFLICT)
        except OSError:
            LOG.exception("Ошибка локального запроса")
            message = ("Не удалось переместить отчёт. Закройте его файлы и повторите удаление."
                       if parts[-1:] == ["delete"] else "Не удалось открыть папку или записать файл.")
            self.json({"error": message}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def log_message(self, message, *args):
        LOG.debug(message, *args)


def start_ollama(cfg):
    endpoint = f"http://127.0.0.1:{cfg['ollama_port']}/api/tags"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(endpoint, timeout=2):
            return
    except (OSError, urllib.error.URLError):
        pass
    exe = shutil.which("ollama")
    if not exe:
        LOG.warning("Ollama недоступна. Приложение запущено для просмотра архива.")
        return
    env = {**os.environ, "OLLAMA_HOST": f"127.0.0.1:{cfg['ollama_port']}",
           "OLLAMA_MODELS": str(ROOT / ".cache/ollama"), "OLLAMA_NO_CLOUD": "1",
           "OLLAMA_NUM_PARALLEL": "1", "OLLAMA_MAX_LOADED_MODELS": "1"}
    with (DATA / "ollama.log").open("ab") as log:
        subprocess.Popen([exe, "serve"], stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--no-ollama", action="store_true")
    args = parser.parse_args()
    prepare()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(DATA / "app.log", encoding="utf-8")])
    cfg = read_json(ROOT / "config/settings.json")
    address = f"http://127.0.0.1:{args.port}/"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        if args.open:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(address + "api/reports", timeout=4) as response:
                if "reports" in json.load(response):
                    webbrowser.open(address)
                    return
        raise
    with server:
        server.store = Reports(cfg)
        if not args.no_ollama:
            threading.Thread(target=start_ollama, args=(cfg,), daemon=True).start()
        print(f"Local: {address}", flush=True)
        if args.open:
            threading.Thread(target=webbrowser.open, args=(address,), daemon=True).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
