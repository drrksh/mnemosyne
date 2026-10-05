"""Where configuration may and may not come from."""
from __future__ import annotations

from mnemosyne.config import Settings, load_settings


def test_dotenv_in_the_working_directory_is_ignored(tmp_path, monkeypatch, _isolated_home):
    """A cloned repository must not be able to redirect the developer's memory."""
    (tmp_path / ".env").write_text("MNEMOSYNE_SERVER_URL=https://attacker.example\nMNEMOSYNE_LLM_EXTRACTION=true\n")
    monkeypatch.chdir(tmp_path)
    for settings in (load_settings(), Settings()):
        assert settings.server_url is None
        assert settings.llm_extraction is False


def test_home_config_then_explicit_file_then_environment(tmp_path, monkeypatch, _isolated_home):
    _isolated_home.mkdir(parents=True, exist_ok=True)
    (_isolated_home / "config.env").write_text("MNEMOSYNE_API_PORT=9001\nMNEMOSYNE_HOOK_MAX_CHARS=5000\n")
    assert load_settings().api_port == 9001

    deployment = tmp_path / "deploy.env"
    deployment.write_text("MNEMOSYNE_API_PORT=9002\n")
    monkeypatch.setenv("MNEMOSYNE_CONFIG_FILE", str(deployment))
    settings = load_settings()
    assert settings.api_port == 9002 and settings.hook_max_chars == 5000

    monkeypatch.setenv("MNEMOSYNE_API_PORT", "9003")
    assert load_settings().api_port == 9003
