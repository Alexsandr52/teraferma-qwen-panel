#!/usr/bin/env python3
"""Copy the exact GGUF into Teraferma's saveable PyTorch workspace."""
from __future__ import annotations

import argparse
import json
import subprocess
import time

from rental import API, APIError, confirm_stop, reconcile_created, spent_this_month, ssh_args
from store import DATA, ROOT, Store, _read, _write

STATE = DATA / "server_storage.json"
MODEL_SHA = "20b1214a23f0f5f401b6117cbd63336cfff6c07be4df93bd6e374d8fd8d12938"
MODEL_SIZE = 24945124640
REMOTE_MODEL = "/workspace/qwen-panel/Huihui-Qwen3.8-27B-abliterated-Q6_K_L.gguf"


def update(**fields):
    state = _read(STATE, {})
    state.update(fields)
    state["updated_at"] = time.time()
    _write(STATE, state)
    if fields.get("message"):
        print(fields["message"], flush=True)
    return state


def choose_storage_offer(api, profile):
    catalog = api.call("/offers?count=1", retries=1)
    if not catalog.get("live"):
        raise RuntimeError("Каталог машин сейчас недоступен")
    rows = []
    for row in catalog.get("offers", []):
        try:
            if (int(row["num_gpus"]) == 1 and int(row["disk"]) >= 70
                    and float(row["price"]) <= min(profile["max_rate"], 40)
                    and float(row["price"]) > 0 and row["verified"]
                    and float(row["reliability"]) >= 0.99
                    and float(row["net_down"]) >= 500):
                rows.append(row)
        except (KeyError, ValueError, TypeError):
            continue
    if not rows:
        raise RuntimeError("Нет недорогой надёжной GPU для сохранения модели")
    return min(
        rows,
        key=lambda row: float(row["price"]) / 4
        + 25 * float(row.get("traffic_rub") or 0)
    )


def run_ssh(args, command, *, input_data=None, timeout=40):
    result = subprocess.run(
        args + [command], input=input_data, text=True,
        capture_output=True, timeout=timeout
    )
    if result.returncode:
        raise RuntimeError("SSH: " + (result.stderr or result.stdout)[-400:].strip())
    return result.stdout


def wait_ssh(args):
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        try:
            run_ssh(args, "true", timeout=20)
            return
        except (RuntimeError, subprocess.TimeoutExpired):
            time.sleep(5)
    raise RuntimeError("SSH временного сервера не стал доступен")


def launch_download(args):
    script = (ROOT / "remote/store_model.sh").read_text()
    run_ssh(
        args,
        "mkdir -p /workspace/qwen-panel && "
        "cat > /workspace/qwen-panel/store_model.sh && "
        "chmod 700 /workspace/qwen-panel/store_model.sh",
        input_data=script, timeout=60
    )
    run_ssh(
        args,
        "nohup bash /workspace/qwen-panel/store_model.sh "
        "> /workspace/qwen-panel/store_model.log 2>&1 < /dev/null "
        "& echo DOWNLOAD_STARTED",
        timeout=30
    )


def wait_download(args, api, instance_id):
    deadline = time.monotonic() + 3600
    last_api = 0
    while time.monotonic() < deadline:
        try:
            output = run_ssh(
                args,
                "cat /workspace/qwen-panel/storage_status 2>/dev/null; "
                "stat -c %s " + REMOTE_MODEL + " 2>/dev/null || true",
                timeout=25
            ).splitlines()
            status = output[0].strip() if output else ""
            size = int(output[1]) if len(output) > 1 and output[1].isdigit() else 0
            if status == "READY" and size == MODEL_SIZE:
                digest = run_ssh(
                    args, "cat /workspace/qwen-panel/model.sha256", timeout=20
                ).strip().splitlines()[-1]
                if digest != MODEL_SHA:
                    raise RuntimeError("Контрольная сумма модели не совпала")
                update(status="downloaded", bytes=size, message="Модель проверена и готова к сохранению")
                return
            if status == "FAILED":
                detail = run_ssh(
                    args, "tail -n 8 /workspace/qwen-panel/store_model.log",
                    timeout=20
                )
                raise RuntimeError("Загрузка модели завершилась ошибкой: " + detail[-500:])
            update(
                status="downloading", bytes=size,
                message=f"Загружаю модель в сохраняемую папку: {size / MODEL_SIZE:.0%}"
            )
        except (RuntimeError, subprocess.TimeoutExpired) as error:
            if "завершилась ошибкой" in str(error) or "Контрольная сумма" in str(error):
                raise
            update(message="Проверяю соединение со временным сервером")
        if time.monotonic() - last_api > 30:
            instance = api.call(f"/instances/{instance_id}", retries=1)
            update(
                cost=float(instance.get("cost_rub") or 0)
                + float(instance.get("traffic_rub") or 0)
            )
            if instance.get("status") != "running":
                raise RuntimeError(instance.get("error") or "Временный сервер остановился")
            last_api = time.monotonic()
        time.sleep(10)
    raise RuntimeError("Загрузка модели превысила 60 минут")


def save_workspace(api, instance_id):
    state = _read(STATE, {})
    if not state.get("save_requested"):
        update(status="saving", save_requested=True,
               message="Прошу Teraferma сохранить /workspace")
        try:
            response = api.call("/instances/stop", {"id": instance_id, "save": True}, timeout=30)
            update(save_response=response)
        except Exception:
            # A timed-out request may already have started saving. Keep the
            # temporary instance intact until its state can be checked.
            instance = api.call(f"/instances/{instance_id}", retries=1)
            if instance.get("status") not in ("saving", "stopped"):
                raise
    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        instance = api.call(f"/instances/{instance_id}", retries=1)
        update(
            cost=float(instance.get("cost_rub") or 0)
            + float(instance.get("traffic_rub") or 0),
            message=instance.get("stage_note") or "Teraferma сохраняет папку"
        )
        if instance.get("status") == "stopped":
            return instance
        time.sleep(10)
    raise RuntimeError("Teraferma не подтвердила сохранение и остановку")


def migrate(profile_id):
    global STATE
    STATE = DATA / f"server_storage_{profile_id}.json"
    store = Store()
    profile = store.get_profile(profile_id)
    api = API(store.token(profile_id))
    previous = _read(STATE, {})
    instance_id = previous.get("instance_id") if previous.get("profile_id") == profile_id else None
    save_requested = bool(previous.get("save_requested"))
    if instance_id is not None and previous.get("status") in ("error", "stop_failed"):
        old = api.call(f"/instances/{instance_id}", retries=1)
        if old.get("status") == "stopped":
            instance_id = None
            save_requested = False
            update(instance_id=None, save_requested=False)
    update(profile_id=profile_id, status="starting", error=None,
           message="Проверяю место и цену временного сервера")
    try:
        account = api.call("/account", retries=1)
        if not account.get("verified") or not account.get("ssh_key_set"):
            raise RuntimeError("Аккаунт не готов к аренде")
        if instance_id is None:
            offer = choose_storage_offer(api, profile)
            rate = float(offer["price"])
            traffic = float(offer.get("traffic_rub") or 0)
            # The provider can substitute a similar machine with a higher
            # per-GB traffic rate when the listed offer disappears.
            reserve = rate + 25 * max(traffic, 1) + 10
            if spent_this_month(api) + reserve > profile["monthly_limit"]:
                raise RuntimeError("Перенос превысит месячный лимит расходов")
            if float(account.get("balance_rub") or 0) < reserve + 80:
                raise RuntimeError("Недостаточно баланса для второго сервера и текущей аренды")
            previous_ids = {
                str(row["id"]) for row in api.call("/instances", retries=1).get("instances", [])
            }
            update(
                previous_ids=sorted(previous_ids), rate=rate,
                status="creating",
                message=f"Заказываю временный {offer['gpu_name']} за {rate:.0f} ₽/час"
            )
            try:
                created = api.call("/instances", {
                    "offer_id": offer["id"], "template": "pytorch",
                    "disk": 70, "ws_restore": True
                }, timeout=60)
                instance_id = created["id"]
            except APIError as error:
                if 400 <= error.code < 500:
                    raise
                instance_id = reconcile_created(api, previous_ids)
            except Exception:
                instance_id = reconcile_created(api, previous_ids)
            update(instance_id=instance_id)
        if not save_requested:
            deadline = time.monotonic() + 1200
            while time.monotonic() < deadline:
                instance = api.call(f"/instances/{instance_id}", retries=1)
                if instance.get("status") == "running":
                    break
                if instance.get("status") == "stopped":
                    raise RuntimeError(instance.get("error") or "Временный сервер остановился")
                update(status="provisioning",
                       message=instance.get("stage_note") or "Готовится временный сервер")
                time.sleep(5)
            else:
                raise RuntimeError("Временный сервер не подготовился")
            args = ssh_args(store, profile_id, instance["access"]["ssh"])
            wait_ssh(args)
            launch_download(args)
            wait_download(args, api, instance_id)
        stopped = save_workspace(api, instance_id)
        cost = float(stopped.get("cost_rub") or 0) + float(stopped.get("traffic_rub") or 0)
        update(
            status="saved", cost=cost, bytes=MODEL_SIZE,
            saved_at=time.time(), monthly_storage_estimate=MODEL_SIZE / 1e9 * 3,
            message="Модель сохранена в Teraferma; временный сервер остановлен"
        )
    except Exception as error:
        update(status="error", error=str(error), message="Не удалось сохранить модель: " + str(error))
        if instance_id is not None and not _read(STATE, {}).get("save_requested"):
            try:
                stopped = confirm_stop(api, instance_id)
                update(
                    temporary_stopped=True,
                    cost=float(stopped.get("cost_rub") or 0)
                    + float(stopped.get("traffic_rub") or 0),
                    message="Временный сервер остановлен без сохранения; основной Qwen работает"
                )
            except Exception as stop_error:
                update(
                    status="stop_failed", error=str(error) + "; " + str(stop_error),
                    message="Остановка временного сервера НЕ подтверждена"
                )
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile-id", required=True)
    migrate(parser.parse_args().profile_id)
