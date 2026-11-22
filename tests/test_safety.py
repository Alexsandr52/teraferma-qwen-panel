import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import rental
import store


def offer(**changes):
    row = {
        "id": 1, "gpu_name": "RTX A6000", "num_gpus": 1,
        "vram": 48, "ram": 64, "price": 70, "verified": True,
        "reliability": 0.99, "cuda": 12.8, "net_down": 1000
    }
    row.update(changes)
    return row


class SafetyTests(unittest.TestCase):
    def test_rejects_insufficient_or_implausible_gpu(self):
        self.assertTrue(rental.valid_offer(offer(), 80))
        self.assertFalse(rental.valid_offer(offer(vram=32), 80))
        self.assertFalse(rental.valid_offer(offer(price=81), 80))
        self.assertFalse(rental.valid_offer(offer(gpu_name="RTX 4090"), 80))
        self.assertFalse(rental.valid_offer(offer(verified=False), 80))

    def test_ambiguous_creation_is_not_guessed(self):
        api = Mock()
        api.call.return_value = {
            "instances": [
                {"id": 1, "status": "stopped"},
                {"id": 2, "status": "running"},
                {"id": 3, "status": "provisioning"}
            ]
        }
        with self.assertRaises(RuntimeError):
            rental.reconcile_created(api, {"1"})

    def test_stop_waits_for_provider_confirmation(self):
        api = Mock()
        api.call.side_effect = [
            {"status": "running"}, {"ok": True},
            {"status": "stopped", "cost_rub": 12}
        ]
        with patch.object(rental.time, "sleep"):
            result = rental.confirm_stop(api, 123, timeout=5)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(api.call.call_args_list[1].args, ("/instances/stop", {"id": 123}))

    def test_missing_opencode_keeps_model_available(self):
        controller = rental.RentalController(Mock(get_state=Mock(return_value={"status": "idle"})))
        with patch.object(controller, "_open_opencode", side_effect=OSError("OpenCode недоступен")):
            self.assertIn("Открой чат модели", controller._ready_message())

    def test_remote_failure_preserves_engine_error(self):
        database = Mock(get_state=Mock(return_value={"status": "idle"}))
        controller = rental.RentalController(database)
        controller.tunnel = Mock()
        controller.tunnel.poll.return_value = None
        report = ("STAGE:FAILED\nBYTES:24945124640\nBUILD:\n"
                  "LOG:engine diagnostic at the start\n" + "x" * 700 +
                  "\nllama-server завершился при загрузке модели")
        with patch.object(controller, "_health", return_value=False), \
             patch.object(controller, "_ssh_run", return_value=report), \
             patch.object(rental.time, "monotonic", return_value=100):
            with self.assertRaisesRegex(RuntimeError, "engine diagnostic at the start"):
                controller._wait_model(Mock(), 1)

    def test_ssh_auth_failure_survives_timeout(self):
        database = Mock(get_state=Mock(return_value={"status": "idle"}))
        controller = rental.RentalController(database)
        with patch.object(rental, "ssh_args", return_value=["ssh"]), \
             patch.object(controller, "_ssh_run", side_effect=RuntimeError("Permission denied (publickey)")), \
             patch.object(controller.stop_event, "wait"), \
             patch.object(rental.time, "monotonic", side_effect=[0, 0, 151]):
            with self.assertRaisesRegex(RuntimeError, "Сервер отклонил SSH-ключ"):
                controller._connect_ssh("profile", {})

    def test_profiles_keep_tokens_separate_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(store, "DATA", root), \
                 patch.object(store, "SECRETS", root / "secrets"), \
                 patch.object(store, "PROFILES", root / "profiles.json"), \
                 patch.object(store, "STATE", root / "state.json"), \
                 patch.object(store, "ENV_FILE", root / ".env"), \
                 patch.object(store, "RUNTIME_KEYS", root / "runtime"):
                database = store.Store()
                first = database.add_profile("Первый", "tf_live_first", {"email": "a@example.com"})
                again = database.add_profile("Повтор", "tf_live_first", {"email": "a@example.com"})
                self.assertEqual(first["id"], again["id"])
                self.assertEqual(first["public_key"], again["public_key"])
                second = database.add_profile("Второй", "tf_live_second", {"email": "b@example.com"})
                self.assertNotEqual(first["id"], second["id"])
                self.assertNotEqual(first["public_key"], second["public_key"])
                self.assertEqual(database.token(first["id"]), "tf_live_first")
                self.assertEqual(database.token(second["id"]), "tf_live_second")
                self.assertEqual(
                    os.stat(root / ".env").st_mode & 0o777, 0o600
                )
                self.assertFalse((root / "secrets" / first["id"] / "token").exists())
                self.assertFalse((root / "secrets" / first["id"] / "id_ed25519").exists())
                self.assertEqual(os.stat(database.key_path(first["id"])).st_mode & 0o777, 0o600)
                self.assertNotIn("tf_live_", (root / "profiles.json").read_text())
                database.update_state(
                    profile_id=first["id"], instance_id=50,
                    confirmed_stopped=False, status="ready"
                )
                with self.assertRaises(ValueError):
                    database.delete_profile(first["id"])
                database.delete_profile(second["id"])
                self.assertEqual(len(database.list_profiles()), 1)


if __name__ == "__main__":
    unittest.main()
