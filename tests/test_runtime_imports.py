import os
import unittest

os.environ.setdefault("TELEGRAM_TOKEN", "123:test")
os.environ.setdefault("TELEGRAM_OWNER_ID", "1")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault(
    "SUPABASE_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJyb2xlIjoiYW5vbiIsImlhdCI6MTcwMDAwMDAwMCwiZXhwIjoyMDAwMDAwMDAwfQ.c2lnbmF0dXJl",
)
os.environ.setdefault(
    "SESSION_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
)
os.environ.setdefault("VIDEO_API_URL", "https://video.example.test")
os.environ.setdefault("VIDEO_API_SECRET", "test-secret")
os.environ.setdefault("VIDEO_SETTINGS_REMOTE", "false")


class RuntimeImportTests(unittest.TestCase):
    def test_main_runtime_imports_and_all_handlers_register(self):
        from telegram.ext import Application

        import main
        from bot.extra_handlers import register_extra_handlers
        from bot.handlers.contas import register_contas_handlers
        from bot.handlers.dashboard import register_dashboard_handlers
        from bot.handlers.operacoes import register_operacoes_handlers
        from bot.handlers.video import register_video_handlers
        from bot.runtime_guards import register_runtime_guards
        from bot.video_editor_delivery import register_video_editor_delivery_guard

        self.assertTrue(callable(main.main))

        app = Application.builder().token("123:test").build()
        register_video_editor_delivery_guard(app)
        register_runtime_guards(app)
        register_dashboard_handlers(app)
        register_contas_handlers(app)
        register_operacoes_handlers(app)
        register_extra_handlers(app)
        register_video_handlers(app)

        # Guards precisam existir antes do dashboard/handlers funcionais.
        self.assertIn(-6, app.handlers)
        self.assertIn(-5, app.handlers)
        self.assertIn(-4, app.handlers)
        self.assertIn(-1, app.handlers)
        self.assertIn(0, app.handlers)
        self.assertGreater(sum(len(group) for group in app.handlers.values()), 20)


if __name__ == "__main__":
    unittest.main()
