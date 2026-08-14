import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("SESSION_ENCRYPTION_KEY", "test-key-not-for-production")
os.environ.setdefault("TELEGRAM_OWNER_ID", "1")
os.environ.setdefault("TELEGRAM_TOKEN", "test-token")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-key")

import instagram.client as client_module


class FakeClient:
    login_result = "ok"
    standard_login_result = False

    def __init__(self):
        self.uuids = {}
        self.challenge_code_handler = None

    def set_uuids(self, values):
        self.uuids = values.copy()

    def set_proxy(self, value):
        self.proxy = value

    def set_country(self, value):
        self.country = value

    def set_country_code(self, value):
        self.country_code = value

    def set_locale(self, value):
        self.locale = value

    def set_timezone_offset(self, value):
        self.timezone = value

    def bloks_caa_login_prepare(self, username=""):
        return True

    def bloks_caa_login_send_request(self, password, username=""):
        return {"mode": self.login_result}

    def bloks_apply_login_response(self, result):
        return result.get("mode") == "ok"

    def bloks_caa_login_needs_two_step(self, result):
        return result.get("mode") == "challenge"

    def bloks_extract_two_step_verification_context(self, result):
        return "generic-2fa-context" if result.get("mode") == "generic_2fa" else ""

    def _bloks_all_text(self, result):
        return str(result)

    def bloks_extract_context_data(self, result, step):
        return {"step": step} if result else {}

    def bloks_ap_two_step_verification_entrypoint(self, context):
        return {"entry": context}

    def bloks_ap_two_step_verification_code_entry(self, context):
        return {"code": context}

    def bloks_ap_two_step_verification_submit_code(self, context, code):
        return {"mode": "ok" if code == "123456" else "invalid"}

    def bloks_two_step_verification_entrypoint(self, context):
        return {}

    def bloks_two_step_verification_method_picker(self, context):
        return {}

    def bloks_two_step_verification_select_method(self, context, selected_method):
        return {}

    def bloks_two_step_verification_enter_backup_code(self, context):
        return {}

    def bloks_two_step_verification_verify_code(self, context, code, challenge="totp"):
        return {"mode": "ok" if code == "123456" else "invalid"}

    def dump_settings(self, path):
        Path(path).write_text(json.dumps({"uuids": self.uuids}), encoding="utf-8")

    def load_settings(self, path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def login(self, username, password, verification_code=""):
        if verification_code:
            return verification_code == "123456"
        return self.standard_login_result

    def get_timeline_feed(self):
        return {}


class InstagramClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.sessions_patch = patch.object(client_module, "SESSIONS_DIR", self.temp.name)
        self.client_patch = patch.object(client_module, "Client", FakeClient)
        self.sessions_patch.start()
        self.client_patch.start()
        client_module.PENDING_CHALLENGES.clear()
        FakeClient.login_result = "ok"
        FakeClient.standard_login_result = False

    def tearDown(self):
        self.client_patch.stop()
        self.sessions_patch.stop()
        self.temp.cleanup()

    def test_identity_is_stable_per_account(self):
        first = client_module.InstagramClient("conta", "senha").cl.uuids
        second = client_module.InstagramClient("conta", "outra-senha").cl.uuids
        other = client_module.InstagramClient("outra", "senha").cl.uuids
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)

    def test_caa_login_returns_before_waiting_for_code(self):
        FakeClient.login_result = "challenge"
        ig = client_module.InstagramClient("conta", "senha")
        self.assertEqual(ig.login(), "challenge")
        self.assertIn("conta", client_module.PENDING_CHALLENGES)
        self.assertIsNotNone(ig._caa_send_result)
        self.assertIsNotNone(ig._caa_submit_context)

    def test_valid_caa_code_finishes_login_and_saves_session(self):
        FakeClient.login_result = "challenge"
        ig = client_module.InstagramClient("conta", "senha")
        self.assertEqual(ig.login(), "challenge")
        self.assertEqual(ig.submit_code("123-456"), "ok")
        self.assertTrue(ig.session_path.exists())
        self.assertNotIn("conta", client_module.PENDING_CHALLENGES)

    def test_generic_bloks_two_factor_flow_is_supported(self):
        FakeClient.login_result = "generic_2fa"
        ig = client_module.InstagramClient("conta", "senha")
        self.assertEqual(ig.login(), "two_factor")
        self.assertTrue(ig.submit_2fa("123456"))
        self.assertTrue(ig.session_path.exists())

    def test_caa_password_text_retries_standard_login(self):
        FakeClient.login_result = "incorrect password"
        FakeClient.standard_login_result = True
        ig = client_module.InstagramClient("conta", "senha")
        self.assertEqual(ig.login(), "ok")
        self.assertTrue(ig.session_path.exists())

    def test_caa_password_text_is_not_reported_as_proven_bad_password(self):
        FakeClient.login_result = "incorrect password"
        ig = client_module.InstagramClient("conta", "senha")
        self.assertEqual(ig.login(), "error:credentials_rejected")

    def test_invalid_code_is_rejected(self):
        FakeClient.login_result = "challenge"
        ig = client_module.InstagramClient("conta", "senha")
        ig.login()
        self.assertEqual(ig.submit_code("999999"), "error")

    def test_code_detection_and_preview(self):
        self.assertEqual(client_module.detect_code_type("123-456"), "sms_or_totp")
        self.assertEqual(client_module.detect_code_type("1234-5678"), "backup")
        self.assertEqual(client_module.format_preview("123456"), "123-456")


if __name__ == "__main__":
    unittest.main()
