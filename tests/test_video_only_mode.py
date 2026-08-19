from bot import video_only_mode as mode


class DummyContext:
    def __init__(self, user_data=None):
        self.user_data = user_data or {}


def test_video_context_detects_bulk_editor_state():
    assert mode._video_context(DummyContext({"dl_videos": []})) is True


def test_video_context_detects_dashboard_video_prompt():
    assert mode._video_context(DummyContext({"dashboard_pending": "video_editor"})) is True


def test_video_context_rejects_unrelated_dashboard_prompt():
    assert mode._video_context(DummyContext({"dashboard_pending": "add_target"})) is False


def test_video_command_allowlist_contains_editor_flows_only():
    assert "/download" in mode._VIDEO_COMMANDS
    assert "/video_lote" in mode._VIDEO_COMMANDS
    assert "/alvo_add" not in mode._VIDEO_COMMANDS
    assert "/campanha_nova" not in mode._VIDEO_COMMANDS
