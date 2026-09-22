"""First-use setup, private config precedence and honest provider verification."""

import stat

import pytest
from typer.testing import CliRunner

from weekly_deals.cli import app
from weekly_deals.config import Secrets, Settings, save_jev_credentials, user_credentials_path
from weekly_deals.models.base import ModelError
from weekly_deals.models.jev import JevClassifier


@pytest.fixture
def setup_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TYPESAFE_API_KEY")
    monkeypatch.delenv("TYPESAFE_EMAIL_PROCESSING_CONSENT")
    monkeypatch.setenv("WEEKLY_DEALS_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def successful_probe(self, payload):
    return {
        "model": "jev-test",
        "answers": {"contains_promotion": {"noul": 0.98}},
        "usage": {"input_tokens": 10, "output_tokens": 4},
    }, 0


def rejected_probe(self, payload):
    raise ModelError("Rejected synthetic key", code="auth")


def test_hidden_setup_saves_a_private_key_without_granting_llm_consent(
    monkeypatch,
    setup_directory,
):
    monkeypatch.setattr(JevClassifier, "_post", successful_probe)
    result = CliRunner().invoke(
        app, ["auth", "jev", "--allow-email-processing"], input="synthetic-private-key\n"
    )
    assert result.exit_code == 0, result.output
    assert "synthetic-private-key" not in result.output
    assert "JEV verified: jev-test" in result.output
    assert stat.S_IMODE(user_credentials_path().stat().st_mode) == 0o600
    settings = Settings.build()
    assert settings.secrets.typesafe_api_key.get_secret_value() == "synthetic-private-key"
    assert settings.cloud_consent_satisfied()
    settings.secrets.llm_provider = "openai"
    assert not settings.cloud_consent_satisfied()
    settings.secrets.llm_provider = "mock"
    settings.secrets.jev_endpoint = "https://another-provider.example/api"
    assert not settings.cloud_consent_satisfied()


def test_rejected_key_does_not_replace_saved_credentials(monkeypatch, setup_directory):
    save_jev_credentials("previous-key", "jev-latest", allow_email_processing=False)
    before = user_credentials_path().read_text()
    monkeypatch.setattr(JevClassifier, "_post", rejected_probe)
    result = CliRunner().invoke(
        app, ["auth", "jev", "--no-email-processing"], input="rejected-key\n"
    )
    assert result.exit_code == 1
    assert "auth" in result.output
    assert "rejected-key" not in result.output
    assert user_credentials_path().read_text() == before


def test_settings_work_across_directories_with_explicit_overrides(monkeypatch, setup_directory):
    save_jev_credentials("user-key", "jev-latest", allow_email_processing=False)
    other = setup_directory / "another-project"
    other.mkdir()
    monkeypatch.chdir(other)
    assert Secrets().typesafe_api_key.get_secret_value() == "user-key"
    (other / ".env").write_text("TYPESAFE_API_KEY=project-key\n")
    assert Secrets().typesafe_api_key.get_secret_value() == "project-key"
    monkeypatch.setenv("TYPESAFE_API_KEY", "shell-key")
    assert Secrets().typesafe_api_key.get_secret_value() == "shell-key"
    monkeypatch.delenv("TYPESAFE_API_KEY")
    assert not Secrets(_env_file=None).has_jev()


def test_setup_reports_a_project_override(monkeypatch, setup_directory):
    (setup_directory / ".env").write_text("TYPESAFE_API_KEY=old-project-key\n")
    monkeypatch.setattr(JevClassifier, "_post", successful_probe)
    result = CliRunner().invoke(
        app, ["auth", "jev", "--no-email-processing"], input="new-user-key\n"
    )
    assert result.exit_code == 1
    assert "overrides" in result.output
    assert "new-user-key" not in result.output


def test_doctor_failure_returns_nonzero(monkeypatch, setup_directory):
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-key")
    monkeypatch.setattr(JevClassifier, "_post", rejected_probe)
    result = CliRunner().invoke(app, ["doctor", "--check-apis"])
    assert result.exit_code == 1
    assert "error=auth" in result.output


def test_llm_only_scan_does_not_require_jev_key(setup_directory):
    result = CliRunner().invoke(app, ["scan", "--mode", "llm-only", "--max-messages", "1"])
    assert result.exit_code == 0, result.output
    assert "TYPESAFE_API_KEY is not set" not in result.output


def test_scan_honors_config_mode_unless_cli_mode_is_explicit(setup_directory):
    config = setup_directory / "local.yaml"
    config.write_text('classification:\n  mode: "off"\n')
    args = ["scan", "--config", str(config), "--max-messages", "1"]
    runner = CliRunner()
    assert runner.invoke(app, args).exit_code == 0
    overridden = runner.invoke(app, [*args, "--mode", "jev-observe"])
    assert overridden.exit_code == 2
    assert "TYPESAFE_API_KEY is not set" in overridden.output
