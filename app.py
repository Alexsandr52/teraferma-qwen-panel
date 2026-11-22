#!/usr/bin/env python3
"""Loopback-only web panel for a Teraferma GPU and local OpenCode."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import signal
import subprocess
import threading
import time
import urllib.parse

from rental import APIError, MODEL_PORT, OPENCODE_PORT, RentalController, account_for_token
from store import DATA, ROOT, Store, _read, _write

HOST = "127.0.0.1"
PORT = 8787
CSRF = secrets.token_urlsafe(32)
store = Store()
controller = RentalController(store)
storage_lock = threading.RLock()


def storage_states():
    result = {}
    for path in DATA.glob("server_storage_*.json"):
        state = _read(path, {})
        if state.get("profile_id"):
            result[state["profile_id"]] = {
                key: state.get(key) for key in
                ("status", "message", "error", "instance_id", "bytes", "cost",
                 "rate", "monthly_storage_estimate", "saved_at")
            }
    legacy = _read(DATA / "server_storage.json", {})
    if legacy.get("profile_id") and legacy["profile_id"] not in result:
        result[legacy["profile_id"]] = {
            key: legacy.get(key) for key in
            ("status", "message", "error", "instance_id", "bytes", "cost",
             "rate", "monthly_storage_estimate", "saved_at")
        }
    return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format, *args):
        # Never log HTTP request bodies, which can contain an API token.
        return

    def _valid_host(self):
        return self.headers.get("Host") in (f"{HOST}:{PORT}", f"localhost:{PORT}")

    def _send(self, code, body, content_type, *, cache=False):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "public, max-age=3600" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, value, code=200):
        self._send(
            code, json.dumps(value, ensure_ascii=False).encode(),
            "application/json; charset=utf-8"
        )

    def do_GET(self):
        if not self._valid_host():
            return self._json({"error": "Некорректный адрес"}, 403)
        path = urllib.parse.urlsplit(self.path)
        if path.path == "/api/health":
            return self._json({"ok": True})
        if path.path == "/api/state":
            opencode = controller.opencode
            return self._json({
                "profiles": store.list_profiles(),
                "rental": store.get_state(),
                "storage": storage_states(),
                "opencode_url": (f"http://127.0.0.1:{OPENCODE_PORT}"
                                 if opencode and opencode.poll() is None else None),
                "model_url": f"http://127.0.0.1:{MODEL_PORT}"
            })
        if path.path == "/api/account":
            profile_id = urllib.parse.parse_qs(path.query).get("profile_id", [""])[0]
            try:
                store.get_profile(profile_id)
                account = account_for_token(store.token(profile_id))
                return self._json({
                    "balance_rub": account.get("balance_rub"),
                    "verified": account.get("verified"),
                    "ssh_key_set": account.get("ssh_key_set")
                })
            except (ValueError, APIError, RuntimeError) as error:
                return self._json({"error": str(error)}, 400)
        if path.path == "/api/offers":
            profile_id = urllib.parse.parse_qs(path.query).get("profile_id", [""])[0]
            try:
                return self._json(controller.offers(profile_id))
            except (ValueError, APIError, RuntimeError) as error:
                return self._json({"error": str(error)}, 400)
        if path.path == "/":
            html = (ROOT / "static/index.html").read_text().replace(
                "APP_CSRF_TOKEN", CSRF
            ).encode()
            return self._send(200, html, "text/html; charset=utf-8")
        static = {
            "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/static/style.css": ("style.css", "text/css; charset=utf-8"),
        }.get(path.path)
        if static:
            filename, mime = static
            return self._send(200, (ROOT / "static" / filename).read_bytes(), mime, cache=True)
        self._json({"error": "Не найдено"}, 404)

    def do_POST(self):
        if not self._valid_host():
            return self._json({"error": "Некорректный адрес"}, 403)
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"):
            return self._json({"error": "Запрос с другого сайта запрещён"}, 403)
        if self.headers.get("X-App-Token") != CSRF:
            return self._json({"error": "Обнови страницу и повтори действие"}, 403)
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self._json({"error": "Нужен JSON"}, 415)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 100000:
                raise ValueError("Слишком большой запрос")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("Неверный формат запроса")
            path = urllib.parse.urlsplit(self.path).path
            if path == "/api/profiles":
                token = str(body.get("token") or "").strip()
                account = account_for_token(token)
                result = store.add_profile(
                    body.get("label"), token, account,
                    max_rate=body.get("max_rate", 80),
                    max_minutes=body.get("max_minutes", 60),
                    monthly_limit=body.get("monthly_limit", 3000)
                )
                return self._json({"profile": result, "account": {
                    "verified": account.get("verified"),
                    "ssh_key_set": account.get("ssh_key_set")
                }})
            if path == "/api/import-existing":
                source = Path.home() / ".config/qwen-rental"
                token = (source / "token").read_text().strip()
                account = account_for_token(token)
                result = store.find_by_token(token)
                if result is None:
                    result = store.add_profile(
                        body.get("label") or "Текущий аккаунт", token, account,
                        key_source=(source / "id_ed25519", source / "id_ed25519.pub")
                    )
                return self._json({"profile": result, "account": {
                    "verified": account.get("verified"),
                    "ssh_key_set": account.get("ssh_key_set")
                }})
            if path == "/api/profiles/delete":
                store.delete_profile(str(body.get("id") or ""))
                return self._json({"ok": True})
            if path == "/api/start":
                controller.start(str(body.get("profile_id") or ""), body.get("offer_id"))
                return self._json({"ok": True})
            if path == "/api/stop":
                controller.stop()
                return self._json({"ok": True})
            if path == "/api/keep-running":
                controller.keep_running()
                return self._json({"ok": True})
            if path == "/api/reconnect":
                controller.reconnect()
                return self._json({"ok": True})
            if path == "/api/save-on-server":
                profile_id = str(body.get("profile_id") or "")
                store.get_profile(profile_id)
                with storage_lock:
                    current = storage_states().get(profile_id, {})
                    if current.get("status") == "saved":
                        raise ValueError("Модель уже сохранена на этом аккаунте")
                    if current.get("status") in (
                        "starting", "creating", "provisioning", "downloading",
                        "downloaded", "saving"
                    ):
                        raise ValueError("Сохранение уже выполняется")
                    state_path = DATA / f"server_storage_{profile_id}.json"
                    pending = _read(state_path, {})
                    pending.update(profile_id=profile_id, status="starting",
                                   message="Запускаю перенос модели", error=None,
                                   updated_at=time.time())
                    _write(state_path, pending)
                    try:
                        unit = "teraferma-qwen-save-" + secrets.token_hex(4)
                        subprocess.run([
                            "systemd-run", "--user", "--collect", "--unit=" + unit,
                            "--property=WorkingDirectory=" + str(ROOT),
                            "/usr/bin/python3", str(ROOT / "migrate_model.py"),
                            "--profile-id", profile_id
                        ], check=True, capture_output=True, text=True)
                    except (OSError, subprocess.CalledProcessError) as error:
                        pending.update(status="error", error=str(error),
                                       message="Не удалось запустить перенос")
                        _write(state_path, pending)
                        raise RuntimeError("Не удалось запустить перенос") from error
                return self._json({"ok": True})
            return self._json({"error": "Не найдено"}, 404)
        except (ValueError, FileNotFoundError, APIError, RuntimeError) as error:
            return self._json({"error": str(error)}, 400)
        except Exception:
            return self._json({"error": "Внутренняя ошибка панели"}, 500)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    def on_signal(_signum, _frame):
        if store.get_state().get("keep_running"):
            controller._cleanup_processes()
            raise SystemExit(0)
        controller.stop()
        if controller.thread is not None:
            controller.thread.join(timeout=190)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    Server((HOST, PORT), Handler).serve_forever(poll_interval=0.5)
