"""The rename must not disconnect existing data or environment settings."""

import pytest

from weekly_deals.config import Secrets


@pytest.mark.parametrize("suffix", ["DATA_DIR", "EML_DIR", "TIMEZONE", "LANGUAGE"])
def test_new_environment_names_override_legacy_names(monkeypatch, suffix):
    monkeypatch.setenv(f"MEALDEALS_{suffix}", "legacy-value")
    monkeypatch.delenv(f"WEEKLY_DEALS_{suffix}", raising=False)
    field = f"weekly_deals_{suffix.lower()}"
    assert getattr(Secrets(_env_file=None), field) == "legacy-value"

    monkeypatch.setenv(f"WEEKLY_DEALS_{suffix}", "new-value")
    assert getattr(Secrets(_env_file=None), field) == "new-value"


def test_existing_data_and_oauth_token_remain_accessible(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.delenv("MEALDEALS_DATA_DIR", raising=False)
    monkeypatch.delenv("WEEKLY_DEALS_DATA_DIR", raising=False)
    settings = Secrets(_env_file=None)
    assert settings.data_dir == tmp_path / "weekly-deals"

    legacy = tmp_path / "mealdeals"
    legacy.mkdir()
    (legacy / "gmail_token.json").write_text('{"synthetic": true}')
    assert settings.data_dir == legacy
    assert (settings.data_dir / "gmail_token.json").read_text() == '{"synthetic": true}'

    current = tmp_path / "weekly-deals"
    current.mkdir()
    assert settings.data_dir == current


def test_existing_dotenv_file_is_still_read(monkeypatch, tmp_path):
    monkeypatch.delenv("MEALDEALS_LANGUAGE", raising=False)
    monkeypatch.delenv("WEEKLY_DEALS_LANGUAGE", raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text("MEALDEALS_LANGUAGE=zh-CN\n")
    assert Secrets(_env_file=dotenv).weekly_deals_language == "zh-CN"
    monkeypatch.setenv("WEEKLY_DEALS_LANGUAGE", "en-US")
    assert Secrets(_env_file=dotenv).weekly_deals_language == "en-US"
