import os
import unittest

os.environ.setdefault("TELEGRAM_TOKEN", "123:test")
os.environ.setdefault("TELEGRAM_OWNER_ID", "1")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-key")
os.environ.setdefault(
    "SESSION_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
)
os.environ.setdefault("VIDEO_API_URL", "https://video.example.test")
os.environ.setdefault("VIDEO_API_SECRET", "test-secret")
os.environ.setdefault("VIDEO_SETTINGS_REMOTE", "false")


class RuntimeImportTests(unittest.TestCase):
    def test_main_runtime_imports_with_pinned_dependencies(self):
        import main
        from bot.runtime_guards import register_runtime_guards
        from bot.extra_handlers import register_extra_handlers

        self.assertTrue(callable(main.main))
        self.assertTrue(callable(register_runtime_guards))
        self.assertTrue(callable(register_extra_handlers))


if __name__ == "__main__":
    unittest.main()
