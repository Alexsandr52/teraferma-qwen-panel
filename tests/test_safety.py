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
