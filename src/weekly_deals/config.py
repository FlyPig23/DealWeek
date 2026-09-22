"""Configuration loading.

Layering, lowest precedence first: built-in defaults -> ``config.yaml`` ->
environment / ``.env`` -> explicit CLI flags.

Secrets live in environment variables or private dotenv files. They use ``SecretStr`` and
never serialised into reports, logs or model prompts.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import set_key
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .schemas import Channel, Preferences

DEFAULT_JEV_MODEL = "jev-latest"
JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"


def user_credentials_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "weekly-deals" / ".env"


def save_jev_credentials(api_key: str, model: str, *, allow_email_processing: bool) -> Path:
    """Save locally after verification, without putting credentials in a skill/repo."""
    path = user_credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".env-")
    staged = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            if path.exists():
                stream.write(path.read_text(encoding="utf-8"))
        for name, value in {
            "TYPESAFE_API_KEY": api_key,
            "JEV_MODEL": model,
            "JEV_ENDPOINT": JEV_ENDPOINT,
            "TYPESAFE_EMAIL_PROCESSING_CONSENT": str(allow_email_processing).lower(),
        }.items():
            set_key(staged, name, value)
        staged.chmod(0o600)
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)
    return path


class MailConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["fixtures", "eml_dir", "gmail_api", "gmail_mcp"] = "fixtures"
    # For provider=eml_dir: a folder of exported .eml files. The rung between
    # the synthetic demo and a live mailbox -- real mail, no credentials.
    eml_dir: str | None = None
    # Stable identity for one exported mailbox; used only by eml_dir.
    account_alias: str = Field(default="local-eml", min_length=1)
    lookback_days: int = Field(default=90, ge=1, le=3650)
    base_query: str = "category:promotions"
    extra_queries: list[str] = Field(default_factory=list)
    include_spam_trash: bool = False
    max_messages: int | None = Field(default=None, ge=1)


class ClassificationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["off", "observe", "gate"] = "observe"
    reject_below: float = Field(default=0.05, ge=0.0, le=1.0)
    accept_above: float = Field(default=0.70, ge=0.0, le=1.0)
    gate_evaluation_record: str | None = Field(
        default=None,
        description="Path to the evaluation record that authorises gate mode. "
        "Without it, gate mode refuses to run.",
    )

    @model_validator(mode="after")
    def _thresholds_ordered(self) -> ClassificationConfig:
        if self.reject_below > self.accept_above:
            raise ValueError("reject_below must not exceed accept_above")
        return self


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mail_concurrency: int = Field(
        default=3,
        ge=1,
        le=16,
        description="Parallel message fetches. Honoured only when the mail source "
        "declares supports_concurrent_fetch; gmail_api currently does not.",
    )
    # Model calls during scans remain sequential. Promotion deduplication honors
    # jev_concurrency and reserves estimated cost before each parallel request.
    # llm_concurrency remains reserved until extraction has equivalent accounting.
    jev_concurrency: int = Field(default=3, ge=1, le=16)
    llm_concurrency: int = Field(default=2, ge=1, le=16)
    max_retries: int = Field(default=2, ge=0, le=8)
    request_timeout_seconds: float = Field(default=60.0, gt=0)
    per_run_budget_usd: float = Field(default=1.00, ge=0.0)
    # Per-million-token prices for the configured LLM. They are account- and
    # model-specific, so there is no safe default -- and without them a call's
    # cost is *unknown*, not zero. A budget cannot be enforced against unknown
    # spend, so `preflight` refuses that combination rather than letting the cap
    # silently do nothing.
    llm_price_input_usd_per_mtok: float | None = Field(default=None, ge=0.0)
    llm_price_output_usd_per_mtok: float | None = Field(default=None, ge=0.0)
    automatic_fallback: bool = Field(
        default=False,
        description="When false, a classifier failure parks the message instead of "
        "silently spending LLM budget.",
    )


class PrivacyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cloud_processing_consent: bool = Field(
        default=False,
        description="Gmail read authorisation and consent to send email text to a "
        "cloud model are two separate decisions.",
    )
    # Stored email text is deleted this many days after a message was last
    # seen. Text still backing a live offer is kept regardless, because the
    # validator's quotes have to stay checkable; 0 disables pruning.
    payload_retention_days: int = Field(default=180, ge=0)
    remote_images: bool = False
    follow_promotion_links: bool = False
    telemetry: bool = False
    redact_personal_identifiers: bool = True


class ReportConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: str = "zh-CN"
    timezone: str = "America/Chicago"
    planning_horizon_days: int = Field(default=14, ge=1, le=120)
    formats: list[Literal["html", "markdown", "json"]] = Field(
        default_factory=lambda: ["html", "markdown", "json"]
    )


class AppConfig(BaseModel):
    """File-backed business configuration (no secrets)."""

    model_config = ConfigDict(extra="forbid")

    mail: MailConfig = Field(default_factory=MailConfig)
    classification: ClassificationConfig = Field(default_factory=ClassificationConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    preferences: Preferences = Field(default_factory=Preferences)

    @classmethod
    def load(cls, path: str | Path | None = None) -> AppConfig:
        if path is None:
            return cls()
        file_path = Path(path).expanduser()
        if not file_path.exists():
            raise FileNotFoundError(f"config file not found: {file_path}")
        raw: dict[str, Any] = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
        if "preferences" in raw and isinstance(raw["preferences"], dict):
            channels = raw["preferences"].get("allowed_channels")
            if channels:
                raw["preferences"]["allowed_channels"] = [Channel(c) for c in channels]
        return cls.model_validate(raw)


class Secrets(BaseSettings):
    """Environment-backed credentials. Never logged, never sent to a model."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    typesafe_api_key: SecretStr | None = None
    # This permission covers the official TypeSafe endpoint only, never another LLM.
    typesafe_email_processing_consent: bool = False
    jev_model: str = DEFAULT_JEV_MODEL
    jev_endpoint: str = JEV_ENDPOINT

    llm_provider: Literal["mock", "openai", "compatible"] = "mock"
    llm_api_key: SecretStr | None = None
    llm_model: str | None = None
    llm_base_url: str | None = None

    mail_provider: Literal["fixtures", "eml_dir", "gmail_api", "gmail_mcp"] | None = None
    weekly_deals_eml_dir: str | None = Field(
        default=None, validation_alias=AliasChoices("weekly_deals_eml_dir", "mealdeals_eml_dir")
    )
    gmail_client_secret_path: str | None = None

    # New names take priority; existing .env files remain usable after an upgrade.
    weekly_deals_data_dir: str | None = Field(
        default=None, validation_alias=AliasChoices("weekly_deals_data_dir", "mealdeals_data_dir")
    )
    weekly_deals_timezone: str | None = Field(
        default=None, validation_alias=AliasChoices("weekly_deals_timezone", "mealdeals_timezone")
    )
    weekly_deals_language: str | None = Field(
        default=None, validation_alias=AliasChoices("weekly_deals_language", "mealdeals_language")
    )

    def __init__(self, **values: Any) -> None:
        # Resolve at runtime so XDG_CONFIG_HOME works in any host. Explicit
        # _env_file=None still disables dotenv loading for callers/tests.
        values.setdefault("_env_file", (user_credentials_path(), Path(".env")))
        super().__init__(**values)

    @property
    def data_dir(self) -> Path:
        if self.weekly_deals_data_dir:
            return Path(self.weekly_deals_data_dir).expanduser()
        base = os.environ.get("XDG_DATA_HOME")
        root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
        current = root / "weekly-deals"
        legacy = root / "mealdeals"
        # Reuse stored mail, decisions and OAuth tokens without moving private data.
        if not current.exists() and legacy.is_dir():
            return legacy
        return current

    def has_jev(self) -> bool:
        return self.typesafe_api_key is not None and bool(
            self.typesafe_api_key.get_secret_value().strip()
        )

    def has_llm(self) -> bool:
        if self.llm_provider == "mock":
            return True
        return (
            self.llm_api_key is not None
            and bool(self.llm_api_key.get_secret_value().strip())
            and bool(self.llm_model)
        )


class Settings(BaseModel):
    """Everything the application needs, assembled and validated once."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    app: AppConfig
    secrets: Secrets
    data_dir: Path
    offline: bool = False

    @classmethod
    def build(
        cls,
        config_path: str | Path | None = None,
        *,
        offline: bool = False,
        overrides: dict[str, Any] | None = None,
    ) -> Settings:
        app = AppConfig.load(config_path)
        secrets = Secrets()

        # Environment wins over the config file for these three.
        if secrets.mail_provider:
            app.mail.provider = secrets.mail_provider
        if secrets.weekly_deals_eml_dir:
            app.mail.eml_dir = secrets.weekly_deals_eml_dir
            app.mail.provider = "eml_dir"
        if secrets.weekly_deals_timezone:
            app.report.timezone = secrets.weekly_deals_timezone
            app.preferences.timezone = secrets.weekly_deals_timezone
        if secrets.weekly_deals_language:
            app.report.language = secrets.weekly_deals_language
            app.preferences.display_language = secrets.weekly_deals_language

        for dotted, value in (overrides or {}).items():
            if value is None:
                continue
            target: Any = app
            parts = dotted.split(".")
            for part in parts[:-1]:
                target = getattr(target, part)
            setattr(target, parts[-1], value)

        data_dir = secrets.data_dir
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return cls(app=app, secrets=secrets, data_dir=data_dir, offline=offline)

    @property
    def store_path(self) -> Path:
        """Where the JSON store lives. A directory of files, not a database."""
        return self.data_dir / "store"

    def cloud_recipients(self) -> list[str]:
        """Providers that would receive the user's email text on this run.

        JEV counts. It is a remote model that is sent the subject and body, so
        gating consent on the *extractor* alone let real mail leave the machine
        whenever the LLM was still set to ``mock`` -- which is the default.
        """
        if self.offline:
            return []
        recipients: list[str] = []
        if self.app.classification.mode in {"observe", "gate"} and self.secrets.has_jev():
            recipients.append(f"JEV ({self.secrets.jev_endpoint})")
        if self.secrets.llm_provider != "mock":
            recipients.append(f"LLM ({self.secrets.llm_provider})")
        return recipients

    def budget_enforceable(self) -> bool:
        """Whether ``per_run_budget_usd`` can actually be checked against spend.

        A limit is only meaningful if every paid call reports a cost. The LLM
        adapters can only price a call when per-token prices are configured, so
        without them the cap would sit there looking like a safety net while
        counting every extraction as $0.00.
        """
        if self.offline or self.app.runtime.per_run_budget_usd <= 0:
            return True
        if self.secrets.llm_provider == "mock":
            return True
        return (
            self.app.runtime.llm_price_input_usd_per_mtok is not None
            and self.app.runtime.llm_price_output_usd_per_mtok is not None
        )

    def cloud_consent_satisfied(self) -> bool:
        if self.app.privacy.cloud_processing_consent:
            return True
        return (
            self.secrets.typesafe_email_processing_consent
            and self.secrets.jev_endpoint == JEV_ENDPOINT
            and self.secrets.llm_provider == "mock"
        )

    def preflight(self) -> list[str]:
        """Return blocking problems for the requested mode. Empty means go."""
        problems: list[str] = []
        mode = self.app.classification.mode

        if mode == "gate" and not self.app.classification.gate_evaluation_record:
            problems.append(
                "classification.mode=gate requires gate_evaluation_record; "
                "run an evaluation and record the thresholds first."
            )
        if mode in {"observe", "gate"} and not self.offline and not self.secrets.has_jev():
            problems.append(
                "TYPESAFE_API_KEY is not set but JEV classification is enabled. "
                "Run `weekly-deals auth jev` in your terminal, or choose --offline explicitly."
            )
        if not self.offline and not self.secrets.has_llm():
            problems.append("LLM_API_KEY / LLM_MODEL are not set for the selected provider.")
        # Reading local .eml files costs nothing and leaks nothing, but sending
        # their text to a cloud model is still a transfer of the user's mail.
        # The gate is on the provider that receives it, not on where it came from
        # -- and *every* remote model counts, the classifier included.
        recipients = self.cloud_recipients()
        if (
            self.app.mail.provider != "fixtures"
            and recipients
            and not self.cloud_consent_satisfied()
        ):
            problems.append(
                "privacy.cloud_processing_consent is false: real email text may not be "
                "sent to " + ", ".join(recipients) + " yet. "
                "For JEV alone, run `weekly-deals auth jev` and choose email processing."
            )
        if not self.budget_enforceable():
            problems.append(
                f"runtime.per_run_budget_usd is ${self.app.runtime.per_run_budget_usd:.2f} but "
                "llm_price_input_usd_per_mtok / llm_price_output_usd_per_mtok are not set, so "
                "extraction spend cannot be measured and the cap would never fire. Set both "
                "prices for your model, or set per_run_budget_usd: 0 to run without a cap."
            )
        if self.app.mail.provider == "gmail_api" and not self.secrets.gmail_client_secret_path:
            problems.append("GMAIL_CLIENT_SECRET_PATH is not set for the gmail_api provider.")
        if self.app.mail.provider == "eml_dir" and not self.app.mail.eml_dir:
            problems.append(
                "mail.provider=eml_dir needs mail.eml_dir (or WEEKLY_DEALS_EML_DIR / --mail-dir)."
            )
        return problems
