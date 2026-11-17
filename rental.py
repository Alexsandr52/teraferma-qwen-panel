"""Teraferma API and the single active GPU rental controller."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from store import DATA, ROOT, Store, _read, _write

API_BASE = "https://teraferma.io/api"
MODEL_PORT = 17862
OPENCODE_PORT = 4100
ACTIVE = {"starting", "creating", "provisioning", "loading", "ready", "stopping",
          "recovering", "creation_uncertain", "stop_failed", "needs_attention"}


class APIError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"Teraferma ({code}): {message}")


class API:
    def __init__(self, token):
        self.token = token

    def call(self, path, body=None, *, retries=0, timeout=25):
        for attempt in range(retries + 1):
            request = urllib.request.Request(
                API_BASE + path,
                data=json.dumps(body).encode() if body is not None else None,
                headers={"Authorization": "Bearer " + self.token,
                         "Content-Type": "application/json",
                         "User-Agent": "Teraferma-Qwen-Panel/1.0"},
            )
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                try:
                    detail = json.loads(error.read()).get("error", "Ошибка API")
                except (ValueError, OSError):
                    detail = "Ошибка API"
                if error.code in (429, 502, 503) and attempt < retries:
                    delay = int(error.headers.get("Retry-After") or 10)
                    time.sleep(min(max(delay, 1), 30))
                    continue
                raise APIError(error.code, detail) from None
            except urllib.error.URLError as error:
                if attempt < retries:
                    time.sleep(5)
                    continue
                raise RuntimeError("Не удалось связаться с Teraferma: " + str(error.reason)) from None


def account_for_token(token):
    return API(token).call("/account", retries=1)


def valid_offer(offer, max_rate):
    try:
        # A few catalog entries report impossible VRAM for consumer cards.
        name = str(offer.get("gpu_name") or "").lower()
        # The saved CUDA runtime is built for the RTX A6000 (sm_86).
        if "rtx a6000" not in name:
            return False
        if any(model in name for model in ("rtx 3090", "rtx 4090", "rtx 5090")):
            return False
        return (
            int(offer["num_gpus"]) == 1
            and int(offer["vram"]) >= 48
            and int(offer["ram"]) >= 32
            and float(offer["price"]) <= max_rate
            and float(offer["price"]) > 0
            and bool(offer["verified"])
            and float(offer["reliability"]) >= 0.98
            and float(offer["cuda"]) >= 12.8
            and float(offer["net_down"]) >= 300
        )
    except (KeyError, TypeError, ValueError):
        return False


def spent_this_month(api):
    zone = ZoneInfo("Europe/Moscow")
    now = dt.datetime.now(zone)
    total = 0.0
    for instance in api.call("/instances", retries=1).get("instances", []):
        try:
            when = dt.datetime.fromisoformat(
                str(instance.get("created_at") or "").replace("Z", "+00:00")
            ).astimezone(zone)
        except ValueError:
            continue
        if (when.year, when.month) == (now.year, now.month):
            total += float(instance.get("cost_rub") or 0)
            total += float(instance.get("traffic_rub") or 0)
    return total


def reconcile_created(api, previous_ids):
    new = [
        row for row in api.call("/instances", retries=1).get("instances", [])
        if str(row.get("id")) not in set(map(str, previous_ids))
        and row.get("status") != "stopped"
    ]
    if len(new) == 1:
        return new[0]["id"]
    raise RuntimeError(
        "Нельзя однозначно найти заказанный сервер. Проверь «Мои серверы» в кабинете Teraferma."
    )


def confirm_stop(api, instance_id, *, timeout=180):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            instance = api.call(f"/instances/{instance_id}", retries=1)
            if instance.get("status") == "stopped":
                return instance
            api.call("/instances/stop", {"id": instance_id}, timeout=20)
            last_error = None
        except Exception as error:
            last_error = str(error)
        time.sleep(5)
    raise RuntimeError(
        "Остановка не подтверждена" + (": " + last_error if last_error else "")
    )


def ssh_args(store, profile_id, access):
    host = str(access.get("host", ""))
    user = str(access.get("user", ""))
    port = int(access.get("port", 0))
    if (not re.fullmatch(r"[A-Za-z0-9._-]+", host)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", user)
            or not 1 <= port <= 65535):
        raise RuntimeError("Teraferma вернула некорректные параметры SSH")
    known_hosts = DATA / f"known_hosts_{int(time.time())}_{port}"
    return [
        "ssh", "-T", "-p", str(port), "-i", str(store.key_path(profile_id)),
        "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "UserKnownHostsFile=" + str(known_hosts),
        "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3", "-o", "ForwardAgent=no",
        f"{user}@{host}",
    ]


class RentalController:
    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.tunnel = None
        self.opencode = None
        self.ssh = None
        state = store.get_state()
        if state.get("status") in ACTIVE and not state.get("confirmed_stopped"):
            target = (
                self._recover_and_resume
                if state.get("instance_id") and state.get("status") in
                {"provisioning", "loading", "ready", "needs_attention"}
                else self._recover_and_stop
            )
            self.thread = threading.Thread(target=target, daemon=True)
            self.thread.start()

    def offers(self, profile_id):
        profile = self.store.get_profile(profile_id)
        api = API(self.store.token(profile_id))
        account = api.call("/account", retries=1)
        catalog = api.call("/offers?count=1", retries=1)
        rows = [
            {k: row.get(k) for k in (
                "id", "gpu_name", "vram", "ram", "price", "traffic_rub",
                "country", "reliability", "net_down", "cuda"
            )}
            for row in catalog.get("offers", [])
            if valid_offer(row, profile["max_rate"])
        ]
        rows.sort(key=lambda row: (row["price"], row.get("traffic_rub") or 0))
        return {
            "account": {k: account.get(k) for k in (
                "email", "verified", "ssh_key_set", "balance_rub", "active_instances"
            )},
            "live": catalog.get("live", False), "offers": rows[:30],
        }

    def start(self, profile_id, offer_id):
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                raise ValueError("Уже выполняется запуск или остановка")
            state = self.store.get_state()
            if state.get("status") in ACTIVE and not state.get("confirmed_stopped"):
                raise ValueError("Сначала останови или проверь прежнюю аренду")
            self.store.get_profile(profile_id)
            self.stop_event.clear()
            self.store.update_state(
                profile_id=profile_id, offer_id=int(offer_id), instance_id=None,
                previous_ids=[], status="starting", confirmed_stopped=False,
                cost=0, rate=None, stop_at=None, keep_running=False, error=None,
                message="Проверяю аккаунт и цену", progress_stage=None,
                progress_bytes=0, progress_build=0, progress_total=24945124640
            )
            self.thread = threading.Thread(
                target=self._worker, args=(profile_id, int(offer_id)), daemon=True
            )
            self.thread.start()

    def stop(self):
        with self.lock:
            state = self.store.get_state()
            if state.get("confirmed_stopped") or state.get("status") == "idle":
                return
            self.stop_event.set()
            self.store.update_state(status="stopping", message="Останавливаю аренду")
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._recover_and_stop, daemon=True)
                self.thread.start()

    def keep_running(self):
        with self.lock:
            state = self.store.get_state()
            if state.get("confirmed_stopped") or not state.get("instance_id"):
                raise ValueError("Нет работающей аренды")
            if state.get("status") not in {"provisioning", "loading", "ready", "needs_attention"}:
                raise ValueError("Сейчас нельзя менять автостоп")
            self.store.update_state(stop_at=None, keep_running=True)

    def reconnect(self):
        with self.lock:
            state = self.store.get_state()
            if state.get("status") != "needs_attention" or not state.get("instance_id"):
                raise ValueError("Нет соединения для восстановления")
            if self.thread is not None and self.thread.is_alive():
                raise ValueError("Восстановление уже идёт")
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._recover_and_resume, daemon=True)
            self.thread.start()

    def _check_stop(self):
        if self.stop_event.is_set():
            raise InterruptedError("Остановлено пользователем")
        stop_at = self.store.get_state().get("stop_at")
        if stop_at and time.time() >= stop_at:
            raise InterruptedError("Истёк лимит времени")

    def _ssh_run(self, command, *, input_data=None, timeout=30):
        if self.ssh is None:
            raise RuntimeError("SSH ещё не настроен")
        result = subprocess.run(
            self.ssh + [command], input=input_data, capture_output=True,
            timeout=timeout, text=True
        )
        if result.returncode:
            raise RuntimeError(
                "Ошибка SSH: " + (result.stderr or result.stdout)[-500:].strip()
            )
        return result.stdout

    def _connect_ssh(self, profile_id, access):
        self.ssh = ssh_args(self.store, profile_id, access)
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            self._check_stop()
            try:
                self._ssh_run("true", timeout=20)
                return
            except (RuntimeError, subprocess.TimeoutExpired):
                time.sleep(5)
        raise RuntimeError("SSH не стал доступен за 2,5 минуты")

    def _launch_remote(self):
        profile_id = self.store.get_state().get("profile_id")
        saved = _read(DATA / f"server_storage_{profile_id}.json", {})
        expect_saved = "1" if saved.get("status") == "saved" else "0"
        builder = (ROOT / "remote/build_pytorch.sh").read_text()
        self._ssh_run(
            "mkdir -p /workspace/qwen-panel && "
            "cat > /workspace/qwen-panel/build_pytorch.sh && "
            "chmod 700 /workspace/qwen-panel/build_pytorch.sh",
            input_data=builder, timeout=60
        )
        script = (ROOT / "remote/bootstrap.sh").read_text()
        self._ssh_run(
            "mkdir -p /workspace/qwen-panel && "
            "cat > /workspace/qwen-panel/bootstrap.sh && "
            "chmod 700 /workspace/qwen-panel/bootstrap.sh",
            input_data=script, timeout=60
        )
        self._ssh_run(
            "nohup env QWEN_EXPECT_SAVED=" + expect_saved + " "
            "bash /workspace/qwen-panel/bootstrap.sh "
            "> /workspace/qwen-panel/bootstrap.log 2>&1 < /dev/null "
            "& echo BOOTSTRAP_STARTED",
            timeout=30
        )

    def _start_tunnel(self):
        if self.tunnel is not None and self.tunnel.poll() is None:
            return
        if self.ssh is None:
            raise RuntimeError("Нет параметров SSH")
        args = self.ssh[:-1] + [
            "-N", "-o", "ExitOnForwardFailure=yes",
            "-L", f"127.0.0.1:{MODEL_PORT}:127.0.0.1:17860",
            self.ssh[-1]
        ]
        self.tunnel = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        time.sleep(1)
        if self.tunnel.poll() is not None:
            raise RuntimeError("Не удалось открыть SSH-туннель к модели")

    def _health(self):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{MODEL_PORT}/health", timeout=4
            ) as response:
                return json.load(response).get("status") == "ok"
        except (OSError, ValueError):
            return False

    def _wait_model(self, api, instance_id):
        deadline = time.monotonic() + 2400
        last_status = 0
        last_cost = 0
        while time.monotonic() < deadline:
            self._check_stop()
            if self.tunnel is None or self.tunnel.poll() is not None:
                try:
                    self._start_tunnel()
                except RuntimeError:
                    time.sleep(5)
                    continue
            if self._health():
                return
            if time.monotonic() - last_status > 12:
                try:
                    report = self._ssh_run(
                        "printf 'STAGE:'; cat /workspace/qwen-panel/status 2>/dev/null || true; "
                        "printf 'BYTES:'; stat -c %s "
                        "/workspace/qwen-panel/Huihui-Qwen3.8-27B-abliterated-Q6_K_L.gguf* "
                        "2>/dev/null | sort -nr | head -1; "
                        "printf 'BUILD:'; grep -oE '\\[[[:space:]]*[0-9]+%\\]' "
                        "/workspace/qwen-panel/bootstrap.log 2>/dev/null "
                        "| tail -n 1 | tr -cd '0-9'; printf '\\n'; "
                        "printf 'LOG:'; tail -n 1 /workspace/qwen-panel/bootstrap.log "
                        "2>/dev/null || true",
                        timeout=25
                    )
                    stage_match = re.search(r"(?m)^STAGE:(\w+)", report)
                    bytes_match = re.search(r"(?m)^BYTES:(\d+)", report)
                    build_match = re.search(r"(?m)^BUILD:(\d+)", report)
                    stage = stage_match.group(1) if stage_match else None
                    byte_count = int(bytes_match.group(1)) if bytes_match else 0
                    build_percent = int(build_match.group(1)) if build_match else 0
                    if stage == "FAILED":
                        raise RuntimeError("Подготовка модели завершилась ошибкой: " + report[-500:])
                    names = {
                        "RESTORING": "Восстанавливаю модель из хранилища Teraferma",
                        "DOWNLOADING": "Первая загрузка модели на сервер",
                        "BUILDING": "Готовлю движок модели",
                        "LOADING": "Загружаю модель в видеопамять",
                        "READY": "Модель готова"
                    }
                    if stage:
                        self.store.update_state(
                            progress_stage=stage, progress_bytes=byte_count,
                            progress_build=build_percent,
                            progress_total=24945124640,
                            message=names.get(stage, "Подготовка модели")
                        )
                except RuntimeError as error:
                    if "завершилась ошибкой" in str(error):
                        raise
                last_status = time.monotonic()
            if time.monotonic() - last_cost > 20:
                try:
                    instance = api.call(f"/instances/{instance_id}", retries=1)
                except (APIError, RuntimeError, TimeoutError, ConnectionError):
                    last_cost = time.monotonic()
                    time.sleep(5)
                    continue
                self.store.update_state(
                    cost=float(instance.get("cost_rub") or 0)
                    + float(instance.get("traffic_rub") or 0)
                )
                if instance.get("status") != "running":
                    raise RuntimeError(instance.get("error") or "GPU-сервер остановился")
                last_cost = time.monotonic()
            time.sleep(5)
        raise RuntimeError("Модель не стала готова за 40 минут")

    def _probe_model(self):
        payload = {
            "model": "qwen38-27b",
            "messages": [{"role": "user", "content": "Reply with OK only."}],
            "max_tokens": 32, "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False}
        }
        request = urllib.request.Request(
            f"http://127.0.0.1:{MODEL_PORT}/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            answer = json.load(response)
        if not answer.get("choices"):
            raise RuntimeError("Модель не ответила на проверочный запрос")

    def _open_opencode(self):
        config = {
            "$schema": "https://opencode.ai/config.json",
            "model": "gpu/qwen38-27b",
            "small_model": "gpu/qwen38-27b",
            "enabled_providers": ["gpu"],
            "autoupdate": False,
            "share": "disabled",
            "provider": {
                "gpu": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Teraferma Qwen",
                    "options": {"baseURL": f"http://127.0.0.1:{MODEL_PORT}/v1"},
                    "models": {
                        "qwen38-27b": {
                            "name": "Huihui Qwen3.8 27B Abliterated Q6_K_L",
                            "limit": {"context": 32768, "output": 8192},
                            "tool_call": True, "reasoning": True
                        }
                    }
                }
            }
        }
        config_path = DATA / "opencode.json"
        _write(config_path, config)
        binary = Path.home() / ".local/bin/opencode"
        if not binary.exists():
            raise RuntimeError("OpenCode не установлен в ~/.local/bin/opencode")
        env = os.environ.copy()
        env["OPENCODE_CONFIG"] = str(config_path)
        log = (DATA / "opencode.log").open("ab")
        self.opencode = subprocess.Popen(
            [str(binary), "serve", "--hostname", "127.0.0.1",
             "--port", str(OPENCODE_PORT)],
            env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True
        )
        log.close()
        time.sleep(2)
        if self.opencode.poll() is not None:
            raise RuntimeError("OpenCode завершился при запуске")
        subprocess.Popen(
            ["xdg-open", f"http://127.0.0.1:{OPENCODE_PORT}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    def _monitor_running(self, api, instance_id, actual_rate, profile):
        """Keep the local tunnel alive without ending a paid run on a brief outage."""
        last_check = 0
        last_relaunch = 0
        disconnected = False
        while True:
            self._check_stop()
            if self.tunnel is None or self.tunnel.poll() is not None or not self._health():
                if not disconnected:
                    self.store.update_state(
                        status="recovering", error=None,
                        message="Соединение прервалось; переподключаюсь автоматически"
                    )
                disconnected = True
                if time.monotonic() - last_check > 20:
                    try:
                        instance = api.call(f"/instances/{instance_id}", retries=1)
                        self.store.update_state(
                            cost=float(instance.get("cost_rub") or 0)
                            + float(instance.get("traffic_rub") or 0)
                        )
                        if instance.get("status") == "stopped":
                            break
                        last_check = time.monotonic()
                    except (APIError, RuntimeError, TimeoutError, ConnectionError):
                        pass
                try:
                    if self.tunnel is not None and self.tunnel.poll() is None:
                        self.tunnel.terminate()
                        self.tunnel.wait(timeout=5)
                    self.tunnel = None
                    self._start_tunnel()
                    if not self._health() and time.monotonic() - last_relaunch > 60:
                        try:
                            self._ssh_run(
                                "test -f /workspace/qwen-panel/llama.pid && "
                                "kill -0 $(cat /workspace/qwen-panel/llama.pid)",
                                timeout=20
                            )
                        except RuntimeError:
                            last_relaunch = time.monotonic()
                            self._launch_remote()
                    if self._health():
                        self.store.update_state(
                            status="ready", error=None,
                            message="Qwen готова; соединение восстановлено"
                        )
                        disconnected = False
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                    self.store.update_state(
                        status="recovering",
                        message="Переподключаюсь к GPU: " + str(error)[-180:]
                    )
                time.sleep(5)
                continue
            if disconnected:
                self.store.update_state(
                    status="ready", error=None,
                    message="Qwen готова; соединение восстановлено"
                )
                disconnected = False
            if time.monotonic() - last_check > 20:
                try:
                    instance = api.call(f"/instances/{instance_id}", retries=1)
                    self.store.update_state(
                        cost=float(instance.get("cost_rub") or 0)
                        + float(instance.get("traffic_rub") or 0)
                    )
                    if instance.get("status") != "running":
                        break
                    if float(instance.get("price_rub_hour") or actual_rate) > profile["max_rate"]:
                        raise InterruptedError("Цена сервера превысила установленный предел")
                    if spent_this_month(api) >= profile["monthly_limit"]:
                        raise InterruptedError("Достигнут месячный лимит расходов")
                    last_check = time.monotonic()
                except (APIError, RuntimeError, TimeoutError, ConnectionError) as error:
                    self.store.update_state(
                        message="Qwen работает; Teraferma временно недоступна: "
                        + str(error)[-140:]
                    )
                    time.sleep(5)
            if self.opencode is not None and self.opencode.poll() is not None:
                try:
                    self._open_opencode()
                except (OSError, RuntimeError) as error:
                    self.store.update_state(
                        message="Модель работает; перезапускаю OpenCode: " + str(error)[-140:]
                    )
            time.sleep(2)

    def _worker(self, profile_id, offer_id):
        api = API(self.store.token(profile_id))
        profile = self.store.get_profile(profile_id)
        instance_id = None
        error_text = None
        try:
            account = api.call("/account", retries=1)
            if not account.get("verified"):
                raise RuntimeError("Подтверди почту в кабинете Teraferma")
            if not account.get("ssh_key_set"):
                raise RuntimeError("Добавь публичный SSH-ключ в кабинете Teraferma")
            catalog = api.call("/offers?count=1", retries=1)
            if not catalog.get("live"):
                raise RuntimeError("Каталог машин сейчас недоступен")
            offer = next(
                (row for row in catalog.get("offers", []) if str(row.get("id")) == str(offer_id)),
                None
            )
            if offer is None or not valid_offer(offer, profile["max_rate"]):
                raise RuntimeError("Выбранная GPU больше недоступна или вышла за лимиты")
            spent = spent_this_month(api)
            rate = float(offer["price"])
            traffic = float(offer.get("traffic_rub") or 0)
            reserve = max(rate, rate * profile["max_minutes"] / 60) + 25 * traffic + 10
            if spent + reserve > profile["monthly_limit"]:
                raise RuntimeError("Запуск превысит месячный лимит расходов")
            if float(account.get("balance_rub") or 0) < reserve:
                raise RuntimeError(f"Для запуска нужно минимум {reserve:.0f} ₽ на балансе")
            self._check_stop()
            previous_ids = {
                str(row["id"]) for row in api.call("/instances", retries=1).get("instances", [])
            }
            self.store.update_state(
                status="creating", previous_ids=sorted(previous_ids), rate=rate,
                message=f"Заказываю {offer['gpu_name']} за {rate:.0f} ₽/час"
            )
            self._check_stop()
            try:
                created = api.call("/instances", {
                    "offer_id": offer_id, "template": "pytorch",
                    "disk": 70, "ws_restore": True
                }, timeout=60)
                instance_id = created["id"]
            except APIError as error:
                if 400 <= error.code < 500:
                    raise
                instance_id = reconcile_created(api, previous_ids)
                self.store.update_state(instance_id=instance_id)
                raise RuntimeError("Ответ на заказ потерялся; найденный сервер останавливается")
            except Exception:
                try:
                    instance_id = reconcile_created(api, previous_ids)
                    self.store.update_state(instance_id=instance_id)
                except Exception:
                    self.store.update_state(
                        status="creation_uncertain", confirmed_stopped=False,
                        message="Неясно, был ли создан сервер. Проверь кабинет Teraferma"
                    )
                    raise
                raise RuntimeError("Ответ на заказ потерялся; найденный сервер останавливается")
            self.store.update_state(instance_id=instance_id)
            deadline = time.monotonic() + 1200
            while time.monotonic() < deadline:
                self._check_stop()
                instance = api.call(f"/instances/{instance_id}", retries=1)
                self.store.update_state(
                    status="provisioning",
                    message=instance.get("stage_note") or "Готовится GPU-сервер"
                )
                if instance.get("status") == "stopped":
                    raise RuntimeError(instance.get("error") or "Сервер остановился при подготовке")
                if instance.get("status") == "running":
                    break
                time.sleep(5)
            else:
                raise RuntimeError("Сервер не подготовился за 20 минут")
            actual_rate = float(instance.get("price_rub_hour") or rate)
            if actual_rate > profile["max_rate"]:
                raise RuntimeError("Фактическая цена превысила установленный предел")
            started = instance.get("started_at")
            started_at = (
                dt.datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp()
                if started else time.time()
            )
            self.store.update_state(
                status="loading", rate=actual_rate,
                stop_at=started_at + profile["max_minutes"] * 60,
                message="Проверяю SSH и готовлю модель"
            )
            self._connect_ssh(profile_id, instance["access"]["ssh"])
            self._launch_remote()
            self._start_tunnel()
            self._wait_model(api, instance_id)
            self._check_stop()
            self._probe_model()
            self._open_opencode()
            self.store.update_state(
                status="ready", error=None,
                message="Qwen готова; OpenCode открыт",
                model_sha256="20b1214a23f0f5f401b6117cbd63336cfff6c07be4df93bd6e374d8fd8d12938"
            )
            self._monitor_running(api, instance_id, actual_rate, profile)
        except InterruptedError as error:
            error_text = str(error)
        except Exception as error:
            error_text = str(error)
        finally:
            if instance_id is not None:
                if error_text and self.store.get_state().get("keep_running") and not self.stop_event.is_set():
                    self.store.update_state(
                        status="needs_attention", confirmed_stopped=False,
                        error=error_text,
                        message="Сервер работает, но соединение требует восстановления"
                    )
                else:
                    self.store.update_state(status="stopping", message="Останавливаю платный сервер")
                    try:
                        stopped = confirm_stop(api, instance_id)
                        self.store.update_state(
                            status="stopped", confirmed_stopped=True,
                            cost=float(stopped.get("cost_rub") or 0)
                            + float(stopped.get("traffic_rub") or 0),
                            error=error_text,
                            message="Сервер остановлен. Начисление за GPU прекращено."
                        )
                    except Exception as stop_error:
                        self.store.update_state(
                            status="stop_failed", confirmed_stopped=False,
                            error=(error_text + "; " if error_text else "") + str(stop_error),
                            message="Остановка НЕ подтверждена. Проверь кабинет Teraferma."
                        )
            elif self.store.get_state().get("status") != "creation_uncertain":
                self.store.update_state(
                    status="error", confirmed_stopped=True,
                    error=error_text, message=error_text or "Запуск отменён"
                )
            self._cleanup_processes()

    def _cleanup_processes(self):
        for process in (self.opencode, self.tunnel):
            if process is not None and process.poll() is None:
                process.terminate()
        self.opencode = None
        self.tunnel = None

    def _recover_and_stop(self):
        state = self.store.get_state()
        profile_id = state.get("profile_id")
        if not profile_id:
            self.store.update_state(status="error", confirmed_stopped=True,
                                    message="Не найден аккаунт аренды")
            return
        try:
            api = API(self.store.token(profile_id))
            instance_id = state.get("instance_id")
            if instance_id is None:
                instance_id = reconcile_created(api, state.get("previous_ids", []))
                self.store.update_state(instance_id=instance_id)
            self.store.update_state(status="recovering", message="Проверяю и останавливаю прежний сервер")
            stopped = confirm_stop(api, instance_id)
            self.store.update_state(
                status="stopped", confirmed_stopped=True,
                cost=float(stopped.get("cost_rub") or 0)
                + float(stopped.get("traffic_rub") or 0),
                message="Сервер остановлен. Начисление за GPU прекращено."
            )
        except Exception as error:
            self.store.update_state(
                status="stop_failed", confirmed_stopped=False, error=str(error),
                message="Остановка НЕ подтверждена. Проверь кабинет Teraferma."
            )
        finally:
            self._cleanup_processes()

    def _recover_and_resume(self):
        """Reconnect to a paid server after an unexpected panel restart."""
        state = self.store.get_state()
        profile_id = state["profile_id"]
        instance_id = state["instance_id"]
        api = API(self.store.token(profile_id))
        profile = self.store.get_profile(profile_id)
        error_text = None
        try:
            instance = api.call(f"/instances/{instance_id}", retries=1)
            if instance.get("status") == "stopped":
                self.store.update_state(
                    status="stopped", confirmed_stopped=True,
                    message="Сервер уже остановлен в Teraferma"
                )
                return
            deadline = time.monotonic() + 1200
            while instance.get("status") == "provisioning" and time.monotonic() < deadline:
                self._check_stop()
                self.store.update_state(
                    status="provisioning",
                    message=instance.get("stage_note") or "Восстанавливаю подготовку сервера"
                )
                time.sleep(5)
                instance = api.call(f"/instances/{instance_id}", retries=1)
            if instance.get("status") != "running":
                raise RuntimeError(instance.get("error") or "GPU-сервер недоступен")
            self._check_stop()
            self.store.update_state(status="loading", message="Восстанавливаю соединение с GPU")
            self._connect_ssh(profile_id, instance["access"]["ssh"])
            self._start_tunnel()
            if not self._health():
                try:
                    self._ssh_run(
                        "test -f /workspace/qwen-panel/llama.pid && "
                        "kill -0 $(cat /workspace/qwen-panel/llama.pid)", timeout=20
                    )
                except RuntimeError:
                    self._launch_remote()
            self._wait_model(api, instance_id)
            self._probe_model()
            self._open_opencode()
            self.store.update_state(
                status="ready", error=None,
                message="Qwen готова; OpenCode открыт"
            )
            self._monitor_running(
                api, instance_id,
                float(instance.get("price_rub_hour") or 0), profile
            )
        except InterruptedError as error:
            error_text = str(error)
        except Exception as error:
            error_text = str(error)
        finally:
            if (error_text and self.store.get_state().get("keep_running")
                    and not self.stop_event.is_set()):
                self.store.update_state(
                    status="needs_attention", confirmed_stopped=False,
                    error=error_text,
                    message="Сервер работает, но соединение требует восстановления"
                )
            elif not self.store.get_state().get("confirmed_stopped"):
                self.store.update_state(status="stopping", message="Останавливаю платный сервер")
                try:
                    stopped = confirm_stop(api, instance_id)
                    self.store.update_state(
                        status="stopped", confirmed_stopped=True,
                        cost=float(stopped.get("cost_rub") or 0)
                        + float(stopped.get("traffic_rub") or 0),
                        error=error_text,
                        message="Сервер остановлен. Начисление за GPU прекращено."
                    )
                except Exception as stop_error:
                    self.store.update_state(
                        status="stop_failed", confirmed_stopped=False,
                        error=(error_text + "; " if error_text else "") + str(stop_error),
                        message="Остановка НЕ подтверждена. Проверь кабинет Teraferma."
                    )
            self._cleanup_processes()

