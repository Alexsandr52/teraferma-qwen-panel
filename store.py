"""Local profiles and crash-recoverable rental state."""
from __future__ import annotations

import json
import hmac
import os
import base64
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parent
DATA = ROOT / ".local"
SECRETS = DATA / "secrets"
PROFILES = DATA / "profiles.json"
STATE = DATA / "state.json"
ENV_FILE = ROOT / ".env"
_runtime_base = Path(os.environ.get("XDG_RUNTIME_DIR") or "/dev/shm")
RUNTIME_KEYS = (_runtime_base if _runtime_base.is_dir() and os.access(_runtime_base, os.W_OK)
                else DATA) / f"teraferma-qwen-keys-{os.getuid()}"


def _token_name(profile_id):
    return "TERAFERMA_TOKEN_" + profile_id.upper()


def _key_name(profile_id):
    return "TERAFERMA_SSH_KEY_B64_" + profile_id.upper()


def _read_env():
    values = {}
    try:
        lines = ENV_FILE.read_text().splitlines()
    except FileNotFoundError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if separator and name.isidentifier():
            values[name] = value
    return values


def _write_env(values):
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = ENV_FILE.with_name(".env." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            for name, value in sorted(values.items()):
                if not name.isidentifier() or "\n" in value or "\r" in value:
                    raise ValueError("Некорректная запись в .env")
                stream.write(f"{name}={value}\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(ENV_FILE)
        ENV_FILE.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _read(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    tmp.replace(path)


class Store:
    def __init__(self):
        DATA.mkdir(parents=True, exist_ok=True)
        SECRETS.mkdir(parents=True, exist_ok=True)
        DATA.chmod(0o700)
        SECRETS.chmod(0o700)
        self.lock = threading.RLock()
        self.profiles = _read(PROFILES, [])
        self.state = _read(STATE, {
            "status": "idle", "confirmed_stopped": True, "message": "Сервер не запущен"
        })
        self.env = _read_env()
        if ENV_FILE.exists():
            ENV_FILE.chmod(0o600)
        self._migrate_legacy_secrets()

    def _migrate_legacy_secrets(self):
        changed = False
        for profile in self.profiles:
            profile_id = profile["id"]
            folder = SECRETS / profile_id
            token_file = folder / "token"
            private_file = folder / "id_ed25519"
            if token_file.exists() and _token_name(profile_id) not in self.env:
                self.env[_token_name(profile_id)] = token_file.read_text().strip()
                changed = True
            if private_file.exists() and _key_name(profile_id) not in self.env:
                self.env[_key_name(profile_id)] = base64.b64encode(
                    private_file.read_bytes()
                ).decode("ascii")
                changed = True
        if changed:
            _write_env(self.env)
        persisted = _read_env()
        for profile in self.profiles:
            profile_id = profile["id"]
            if _token_name(profile_id) in persisted and _key_name(profile_id) in persisted:
                (SECRETS / profile_id / "token").unlink(missing_ok=True)
                (SECRETS / profile_id / "id_ed25519").unlink(missing_ok=True)

    def list_profiles(self):
        with self.lock:
            return [self._public(p) for p in self.profiles]

    def _public(self, profile):
        data = dict(profile)
        key = SECRETS / profile["id"] / "id_ed25519.pub"
        data["public_key"] = key.read_text().strip() if key.exists() else ""
        return data

    def get_profile(self, profile_id):
        with self.lock:
            for profile in self.profiles:
                if profile["id"] == profile_id:
                    return dict(profile)
        raise ValueError("Аккаунт не найден")

    def secret_dir(self, profile_id):
        self.get_profile(profile_id)
        return SECRETS / profile_id

    def token(self, profile_id):
        self.get_profile(profile_id)
        value = os.environ.get(_token_name(profile_id)) or self.env.get(_token_name(profile_id))
        if not value:
            raise RuntimeError("API-токен аккаунта не найден в .env")
        return value

    def find_by_token(self, token):
        with self.lock:
            for profile in self.profiles:
                if hmac.compare_digest(self.token(profile["id"]), token):
                    return self._public(profile)
        return None

    def key_path(self, profile_id):
        self.get_profile(profile_id)
        encoded = os.environ.get(_key_name(profile_id)) or self.env.get(_key_name(profile_id))
        if not encoded:
            raise RuntimeError("SSH-ключ аккаунта не найден в .env")
        try:
            private = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise RuntimeError("SSH-ключ в .env поврежден") from error
        if not private.startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----"):
            raise RuntimeError("SSH-ключ в .env имеет неверный формат")
        folder = RUNTIME_KEYS / profile_id
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        folder.chmod(0o700)
        key = folder / "id_ed25519"
        if not key.exists() or key.read_bytes() != private:
            temporary = folder / ("id_ed25519." + uuid.uuid4().hex + ".tmp")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(private)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(key)
            finally:
                temporary.unlink(missing_ok=True)
        key.chmod(0o600)
        return key

    def get_state(self):
        with self.lock:
            return dict(self.state)

    def update_state(self, **fields):
        with self.lock:
            self.state.update(fields)
            self.state["updated_at"] = time.time()
            _write(STATE, self.state)
            return dict(self.state)

    def add_profile(self, label, token, account, *, key_source=None,
                    max_rate=80, max_minutes=60, monthly_limit=3000):
        label = str(label).strip()[:60]
        if not label:
            raise ValueError("Укажи название аккаунта")
        if not token.startswith("tf_live_") or any(c.isspace() for c in token):
            raise ValueError("Нужен API-токен Teraferma вида tf_live_…")
        max_rate = float(max_rate)
        max_minutes = int(max_minutes)
        monthly_limit = float(monthly_limit)
        if not 1 <= max_rate <= 10000 or not 15 <= max_minutes <= 480 or not 1 <= monthly_limit <= 1000000:
            raise ValueError("Проверь лимиты цены, времени и расходов")
        profile_id = uuid.uuid4().hex
        folder = SECRETS / profile_id
        folder.mkdir(mode=0o700)
        try:
            public = folder / "id_ed25519.pub"
            RUNTIME_KEYS.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(dir=RUNTIME_KEYS) as temporary:
                private = Path(temporary) / "id_ed25519"
                if key_source:
                    src_private, src_public = map(Path, key_source)
                    shutil.copyfile(src_private, private)
                    shutil.copyfile(src_public, public)
                    private.chmod(0o600)
                    derived = subprocess.run(
                        ["ssh-keygen", "-y", "-f", str(private)],
                        check=True, capture_output=True, text=True
                    ).stdout.split()[:2]
                    if derived != public.read_text().split()[:2]:
                        raise ValueError("Публичный и закрытый SSH-ключи не совпадают")
                else:
                    subprocess.run(
                        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(private)],
                        check=True, capture_output=True
                    )
                    shutil.copyfile(str(private) + ".pub", public)
                private_value = base64.b64encode(private.read_bytes()).decode("ascii")
            public.chmod(0o644)
            with self.lock:
                self.env[_token_name(profile_id)] = token
                self.env[_key_name(profile_id)] = private_value
                _write_env(self.env)
            profile = {
                "id": profile_id, "label": label, "email": account.get("email", ""),
                "max_rate": max_rate, "max_minutes": max_minutes,
                "monthly_limit": monthly_limit, "created_at": time.time()
            }
            with self.lock:
                self.profiles.append(profile)
                _write(PROFILES, self.profiles)
            return self._public(profile)
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            with self.lock:
                self.env.pop(_token_name(profile_id), None)
                self.env.pop(_key_name(profile_id), None)
                _write_env(self.env)
            raise

    def delete_profile(self, profile_id):
        with self.lock:
            if self.state.get("profile_id") == profile_id and not self.state.get("confirmed_stopped"):
                raise ValueError("Сначала останови аренду этого аккаунта")
            if not any(p["id"] == profile_id for p in self.profiles):
                raise ValueError("Аккаунт не найден")
            self.profiles = [p for p in self.profiles if p["id"] != profile_id]
            _write(PROFILES, self.profiles)
            shutil.rmtree(SECRETS / profile_id)
            shutil.rmtree(RUNTIME_KEYS / profile_id, ignore_errors=True)
            self.env.pop(_token_name(profile_id), None)
            self.env.pop(_key_name(profile_id), None)
            _write_env(self.env)

