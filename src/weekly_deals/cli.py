"""Typer CLI.

Every command here is a thin wrapper over :mod:`weekly_deals.service`. The CLI
formats; it does not decide. The same is true of the web app and the MCP server,
so the three can never drift apart.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from .clock import SystemClock
from .config import JEV_ENDPOINT, Secrets, Settings, save_jev_credentials
from .planning.explanations import explain_all
from .promotions.render import render_calendar
from .reporting import render as reporting
from .schemas import UserStatus
from .service import WeeklyDealsService

app = typer.Typer(
    name="weekly-deals",
    help="Turn promotional email into a local weekly savings plan.",
    no_args_is_help=True,
    add_completion=False,
)
auth_app = typer.Typer(help="Configure JEV or authorise read-only Gmail access.")
app.add_typer(auth_app, name="auth")

_err = typer.style("error", fg=typer.colors.RED, bold=True)
_warn = typer.style("warning", fg=typer.colors.YELLOW, bold=True)
_ok = typer.style("ok", fg=typer.colors.GREEN, bold=True)


def _service(config: str | None, offline: bool) -> WeeklyDealsService:
    settings = Settings.build(config, offline=offline)
    return WeeklyDealsService(settings, SystemClock(settings.app.report.timezone))


def _print_scan(result, language: str) -> None:
    coverage = result.coverage
    typer.echo(f"run {result.run_id} finished: {result.status}")
    if result.providers:
        typer.echo(
            "  source: {mail} | classifier: {classifier} | extractor: {extractor}".format(
                **result.providers
            )
        )
    typer.echo(f"  {reporting.coverage_sentence(coverage)}")
    if result.stages.rejected:
        typer.echo(f"  filtered by the classifier: {len(result.stages.rejected)}")
    if result.stages.review_flagged:
        typer.echo(f"  flagged for review: {len(set(result.stages.review_flagged))}")
    if result.stages.parked:
        typer.echo(f"  {_warn} parked (budget or provider error): {len(result.stages.parked)}")
    if result.stages.errors:
        typer.echo(f"  {_warn} errors: {json.dumps(result.stages.errors, ensure_ascii=False)}")
    if result.dedup.merged_count:
        typer.echo(f"  merged repeat sightings: {result.dedup.merged_count}")
    if result.dedup.suspected_duplicates:
        typer.echo(f"  suspected duplicates to review: {len(result.dedup.suspected_duplicates)}")
    cost = f"${result.cost_usd:.4f}" if result.cost_known else "unknown (provider gave no usage)"
    typer.echo(f"  estimated model cost: {cost}")


@app.command()
def demo(
    output: Annotated[str, typer.Option(help="Where to write the report.")] = "./demo-report.html",
    formats: Annotated[str, typer.Option(help="Comma-separated: html,markdown,json")] = "html",
) -> None:
    """Run the whole pipeline offline on synthetic data. No keys, no network."""
    from .schemas import Preferences

    # Example preferences, used only by the demo. Real runs start with these
    # empty: the application does not invent a budget on the user's behalf.
    demo_preferences = Preferences(
        currency="USD",
        per_meal_budget_minor=1800,
        max_dining_out_per_week=3,
        party_size=2,
    )
    service = WeeklyDealsService.offline(preferences=demo_preferences)
    result = service.sync_promotions(mode="llm-only")
    plan = service.build_meal_plan()
    offers = service.list_food_offers()

    _print_scan(result, service.settings.app.report.language)
    typer.echo("")

    base = Path(output)
    written: list[str] = []
    for fmt in [f.strip() for f in formats.split(",") if f.strip()]:
        suffix = {"html": ".html", "markdown": ".md", "json": ".json"}.get(fmt)
        if suffix is None:
            typer.echo(f"{_warn} unknown format '{fmt}', skipped")
            continue
        target = base if base.suffix == suffix else base.with_suffix(suffix)
        reporting.render_to_file(
            plan, offers, str(target), fmt, language=service.settings.app.report.language
        )
        written.append(str(target))

    typer.echo(
        f"plan: {len(plan.this_week)} this week, {len(plan.next_week)} next week, "
        f"{len(plan.this_month)} later this month, "
        f"{len(plan.needs_confirmation)} needing confirmation"
    )
    for path in written:
        typer.echo(f"  wrote {path}")
    typer.echo(
        "\nAll merchants and offers in this demo are synthetic. No real promotion is represented."
    )


@app.command()
def doctor(
    config: Annotated[str | None, typer.Option(help="Path to config.yaml")] = None,
    offline: Annotated[bool, typer.Option(help="Do not contact any provider.")] = True,
    check_apis: Annotated[
        bool, typer.Option("--check-apis", help="Send one synthetic probe to each provider.")
    ] = False,
) -> None:
    """Check configuration and dependencies."""
    settings = Settings.build(config, offline=offline and not check_apis)
    typer.echo(f"data directory: {settings.data_dir}")
    typer.echo(f"store:          {settings.store_path}")
    typer.echo(f"mail provider:  {settings.app.mail.provider}")
    typer.echo(f"classification: {settings.app.classification.mode}")
    typer.echo(f"llm provider:   {settings.secrets.llm_provider}")
    typer.echo(f"jev key set:    {settings.secrets.has_jev()}")
    typer.echo(f"llm configured: {settings.secrets.has_llm()}")
    typer.echo(f"cloud consent:  {settings.app.privacy.cloud_processing_consent}")
    typer.echo(f"JEV consent:    {settings.secrets.typesafe_email_processing_consent}")

    problems = settings.preflight()
    if problems:
        typer.echo(f"\n{_warn} blocking problems for this configuration:")
        for problem in problems:
            typer.echo(f"  - {problem}")
    else:
        typer.echo(f"\n{_ok} configuration is consistent")

    probe_failed = False
    if check_apis:
        typer.echo("\nprobing providers with synthetic input only...")
        from .schemas import ExtractionStatus, NormalizedEmail

        probe = NormalizedEmail(
            source_id="probe",
            subject="Synthetic lunch coupon",
            normalized_text="Take $4 off a lunch purchase of $12 or more. Pickup only.",
        )
        classifier = None
        try:
            from .service import build_classifier

            classifier = build_classifier(settings)
            if classifier is None:
                typer.echo("  classifier: disabled")
            else:
                result = classifier.classify(probe)
                probe_failed |= bool(result.error_code) or result.meta.provider != "typesafe"
                typer.echo(
                    f"  classifier: {result.meta.provider}/{result.meta.model} -> "
                    f"p={result.contains_promotion if result.contains_promotion is not None else result.contains_food_offer} "
                    f"error={result.error_code}"
                )
        except Exception as exc:
            probe_failed = True
            typer.echo(f"  {_err} classifier probe failed: {type(exc).__name__}")
        finally:
            if classifier is not None and hasattr(classifier, "close"):
                classifier.close()
        try:
            from .service import build_extractor

            extractor = build_extractor(settings)
            result = extractor.extract(probe)
            probe_failed |= result.status == ExtractionStatus.FAILED
            typer.echo(
                f"  extractor: {result.meta.provider}/{result.meta.model} -> "
                f"status={result.status} offers={len(result.offers)}"
            )
        except Exception as exc:
            probe_failed = True
            typer.echo(f"  {_err} extractor probe failed: {type(exc).__name__}")

    raise typer.Exit(1 if problems or probe_failed else 0)


@auth_app.command("jev")
def auth_jev(
    from_env: Annotated[
        bool, typer.Option(help="Use the existing TYPESAFE_API_KEY instead of a hidden prompt.")
    ] = False,
    allow_email_processing: Annotated[
        bool | None,
        typer.Option(
            "--allow-email-processing/--no-email-processing",
            help="Allow sending scanned email subjects/bodies to TypeSafe for classification.",
        ),
    ] = None,
) -> None:
    """Verify JEV with synthetic text and save your key privately for all directories."""
    from .models.jev import JevClassifier
    from .schemas import NormalizedEmail

    secrets = Secrets()
    if secrets.jev_endpoint != JEV_ENDPOINT:
        typer.echo(
            f"{_err} auth jev only configures the official TypeSafe endpoint. "
            "Remove the JEV_ENDPOINT override before using this setup command."
        )
        raise typer.Exit(2)
    if from_env:
        if not secrets.has_jev():
            typer.echo(
                f"{_err} no TYPESAFE_API_KEY found; run weekly-deals auth jev interactively."
            )
            raise typer.Exit(2)
        key = secrets.typesafe_api_key.get_secret_value().strip()
    else:
        typer.echo("Get your own API key at https://console.typesafe.ai. Input is hidden.")
        key = typer.prompt("TypeSafe API key", hide_input=True).strip()
    if not key:
        typer.echo(f"{_err} an API key is required")
        raise typer.Exit(2)

    typer.echo("Checking JEV using synthetic coupon text only (no mailbox access)...")
    with JevClassifier(key, model=secrets.jev_model, timeout=20, max_retries=0) as classifier:
        result = classifier.classify(
            NormalizedEmail(
                source_id="setup-probe",
                subject="Synthetic clothing coupon",
                normalized_text="Take 20% off jackets this week. Use code DEMO20.",
            )
        )
    if result.error_code:
        typer.echo(f"{_err} JEV verification failed: {result.error_code}. No settings saved.")
        if result.error_code == "auth":
            typer.echo("TypeSafe rejected the key. Check or replace it in the TypeSafe console.")
        raise typer.Exit(1)
    typer.echo(
        f"{_ok} JEV verified: {result.meta.model} "
        f"(promotion probability {result.contains_promotion})"
    )

    if allow_email_processing is None:
        allow_email_processing = typer.confirm(
            "Allow future scans to send email subjects/bodies to TypeSafe for JEV "
            "classification? API usage may be billed",
            default=False,
        )
    path = save_jev_credentials(
        key, secrets.jev_model, allow_email_processing=allow_email_processing
    )
    typer.echo(f"{_ok} saved privately to {path} (mode 0600)")
    effective = Secrets()
    if (
        not effective.has_jev()
        or effective.typesafe_api_key.get_secret_value().strip() != key
        or effective.typesafe_email_processing_consent != allow_email_processing
    ):
        typer.echo(
            f"{_warn} a shell variable or the current directory's .env overrides "
            "the saved settings. Update/remove that override before scanning."
        )
        raise typer.Exit(1)
    typer.echo(f"JEV email processing: {'enabled' if allow_email_processing else 'disabled'}")
    typer.echo(
        "Default mode: observe (records judgments, keeps all messages). "
        "Use host-ingest without --offline to include JEV in agent-host scans."
    )


@auth_app.command("gmail")
def auth_gmail(
    config: Annotated[str | None, typer.Option(help="Path to config.yaml")] = None,
) -> None:
    """Authorise read-only Gmail access in a browser and store the token locally."""
    settings = Settings.build(config)
    secret = settings.secrets.gmail_client_secret_path
    if not secret:
        typer.echo(f"{_err} GMAIL_CLIENT_SECRET_PATH is not set")
        raise typer.Exit(2)

    from .mail.gmail_api import SCOPES, build_credentials

    typer.echo(f"requesting scope: {SCOPES[0]}")
    typer.echo("this grants read access to your mailbox at the OAuth layer;")
    typer.echo("Weekly Deals limits what it reads with its own queries.")
    token_path = settings.data_dir / "gmail_token.json"
    build_credentials(secret, token_path)
    typer.echo(f"{_ok} token stored at {token_path} (mode 0600)")


@app.command()
def scan(
    config: Annotated[str | None, typer.Option(help="Path to config.yaml")] = None,
    lookback_days: Annotated[int | None, typer.Option(help="Days of history to search.")] = None,
    max_messages: Annotated[int | None, typer.Option(help="Cap messages this run.")] = None,
    mode: Annotated[
        str | None,
        typer.Option(
            help="llm-only | jev-observe | jev-gate | host-ingest "
            "(host-ingest normalizes and stops, leaving extraction to the caller)"
        ),
    ] = None,
    mail_dir: Annotated[
        str | None,
        typer.Option(
            help="Scan a folder of exported .eml files instead of Gmail. "
            "No OAuth, no keys; combine with --offline to keep everything local."
        ),
    ] = None,
    offline: Annotated[bool, typer.Option(help="Use fixtures and mock models.")] = False,
) -> None:
    """Read mail, classify, extract and store offers."""
    overrides: dict = {}
    if mode is not None and mode not in {"llm-only", "jev-observe", "jev-gate", "host-ingest"}:
        typer.echo(f"{_err} unknown scan mode: {mode}")
        raise typer.Exit(2)
    if mode is not None and mode != "host-ingest":
        overrides["classification.mode"] = {
            "llm-only": "off",
            "jev-observe": "observe",
            "jev-gate": "gate",
        }[mode]
    if lookback_days is not None:
        overrides["mail.lookback_days"] = lookback_days
    if mail_dir is not None:
        overrides["mail.provider"] = "eml_dir"
        overrides["mail.eml_dir"] = mail_dir
    settings = Settings.build(config, offline=offline, overrides=overrides)

    if mode == "jev-gate" and not settings.app.classification.gate_evaluation_record:
        typer.echo(
            f"{_err} jev-gate needs classification.gate_evaluation_record. "
            "Run an evaluation first; until then use jev-observe."
        )
        raise typer.Exit(2)

    problems = [p for p in settings.preflight() if not offline]
    if problems:
        typer.echo(f"{_err} cannot start:")
        for problem in problems:
            typer.echo(f"  - {problem}")
        raise typer.Exit(2)

    service = WeeklyDealsService(settings, SystemClock(settings.app.report.timezone))
    result = service.sync_promotions(mode=mode, max_messages=max_messages)
    _print_scan(result, settings.app.report.language)


@app.command()
def report(
    output: Annotated[str, typer.Option(help="Output path.")] = "./report.html",
    format: Annotated[str, typer.Option(help="html | markdown | json")] = "html",
    config: Annotated[str | None, typer.Option(help="Path to config.yaml")] = None,
    offline: Annotated[bool, typer.Option(help="Use the offline database.")] = False,
) -> None:
    """Build a report from stored offers. Does not call a model or the network."""
    service = _service(config, offline)
    plan = service.build_meal_plan()
    offers = service.list_food_offers()
    if not offers:
        typer.echo(
            f"{_warn} no offers stored yet. This is not the same as 'no offers exist' -- "
            "run `weekly-deals scan` first."
        )
    reporting.render_to_file(
        plan, offers, output, format, language=service.settings.app.report.language
    )
    typer.echo(f"{_ok} wrote {output}")


@app.command("offers")
def list_offers(
    config: Annotated[str | None, typer.Option(help="Path to config.yaml")] = None,
    offline: Annotated[bool, typer.Option()] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """List stored offers with their derived state."""
    service = _service(config, offline)
    offers = service.list_food_offers()
    if as_json:
        typer.echo(
            json.dumps([json.loads(o.model_dump_json()) for o in offers], ensure_ascii=False)
        )
        return
    if not offers:
        typer.echo("no offers stored")
        return
    for offer in offers:
        ends = offer.temporal.ends.date
        typer.echo(
            f"{offer.offer_id}  {offer.merchant[:24]:24s}  "
            f"{offer.time_status:20s}  {offer.eligibility_status:12s}  "
            f"ends {ends.isoformat() if ends else 'unknown'}"
        )


def _show_savings_calendar(
    *,
    output: str | None,
    as_json: bool,
    config: str | None,
    offline: bool,
) -> None:
    service = _service(config, offline)
    events = service.list_promotions()
    if as_json:
        payload = [json.loads(event.model_dump_json()) for event in events]
        rendered = json.dumps(payload, ensure_ascii=False, indent=2)
        default_output = None
    else:
        rendered = render_calendar(events, now=service.clock.now())
        default_output = "./savings-calendar.html"
    target = output or default_output
    if target:
        Path(target).write_text(rendered + ("\n" if as_json else ""), encoding="utf-8")
        typer.echo(f"{_ok} wrote {target}")
    else:
        typer.echo(rendered)


@app.command("calendar")
def calendar(
    output: Annotated[str | None, typer.Option(help="写入 HTML 或 JSON 文件。")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    config: Annotated[str | None, typer.Option()] = None,
    offline: Annotated[bool, typer.Option()] = False,
) -> None:
    """Write every indexed Promotions message as a weekly HTML savings calendar."""
    _show_savings_calendar(output=output, as_json=as_json, config=config, offline=offline)


@app.command("savings")
def savings(
    output: Annotated[str | None, typer.Option(help="写入 HTML 或 JSON 文件。")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    config: Annotated[str | None, typer.Option()] = None,
    offline: Annotated[bool, typer.Option()] = False,
) -> None:
    """Write the overall savings calendar across promotion categories."""
    _show_savings_calendar(output=output, as_json=as_json, config=config, offline=offline)


@app.command("mark")
def mark(
    offer_id: Annotated[str, typer.Argument(help="Offer id from `weekly-deals offers`.")],
    status: Annotated[str, typer.Option(help="used | dismissed | saved | planned")] = "used",
    date: Annotated[str | None, typer.Option(help="Planned date, YYYY-MM-DD.")] = None,
    note: Annotated[str | None, typer.Option()] = None,
    config: Annotated[str | None, typer.Option()] = None,
    offline: Annotated[bool, typer.Option()] = False,
) -> None:
    """Record your own decision about an offer. A re-scan will not overwrite it."""
    service = _service(config, offline)
    try:
        state = service.set_user_state(
            offer_id, status=UserStatus(status), planned_date=date, note=note
        )
    except ValueError as exc:
        typer.echo(f"{_err} {exc}")
        raise typer.Exit(2) from exc
    typer.echo(
        f"{_ok} {offer_id}: {state.status}"
        + (f" on {state.planned_date}" if state.planned_date else "")
    )


@app.command()
def plan(
    config: Annotated[str | None, typer.Option()] = None,
    offline: Annotated[bool, typer.Option()] = False,
) -> None:
    """Show the current plan in the terminal."""
    service = _service(config, offline)
    result = service.build_meal_plan()
    language = service.settings.app.report.language
    sections = [
        ("本周优先", result.this_week),
        ("可以留到下周", result.next_week),
        ("本月其他", result.this_month),
        ("待确认", result.needs_confirmation),
    ]
    for heading, items in sections:
        typer.echo(f"\n== {heading} ({len(items)}) ==")
        for item in items:
            reasons = "、".join(explain_all(item.reason_codes, language)) or "-"
            slot = item.slot_date.isoformat() if item.slot_date else "未指定"
            typer.echo(f"  [{slot}] {item.merchant} — {item.title}")
            typer.echo(f"      {reasons}")
            if item.unknowns:
                typer.echo(f"      待确认：{'；'.join(item.unknowns)}")
    for note in result.notes:
        typer.echo(f"\n{note}")


@app.command()
def pending(
    limit: Annotated[int | None, typer.Option(help="Cap how many messages to emit.")] = None,
    config: Annotated[str | None, typer.Option()] = None,
    offline: Annotated[bool, typer.Option()] = True,
) -> None:
    """Emit stored messages awaiting extraction, as JSON, with their text.

    For a host that reads the mailbox itself -- see `skills/weekly-deals/SKILL.md`.
    Extract against the `normalized_text` printed here and nothing else: it is
    the text the validator checks evidence quotes against.
    """
    service = _service(config, offline)
    typer.echo(json.dumps(service.pending_messages(limit=limit), ensure_ascii=False, indent=2))


@app.command()
def ingest(
    file: Annotated[str, typer.Option(help='JSON: {"<message_id>": [<OfferDraft>, ...]}')],
    config: Annotated[str | None, typer.Option()] = None,
    offline: Annotated[bool, typer.Option()] = True,
) -> None:
    """Take offers a host extracted, validate them, and store what survives.

    The drafts are not trusted. Each one goes through the same validator as any
    model's output: quotes must be locatable in the stored text, an unstated
    deadline stays unknown, and nothing unverified reaches a plan.
    """
    service = _service(config, offline)
    try:
        payload = json.loads(Path(file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        typer.echo(f"{_err} could not read {file}: {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(payload, dict):
        typer.echo(f"{_err} expected an object mapping message ids to draft lists")
        raise typer.Exit(2)

    summary = service.ingest_offers(payload)
    typer.echo(f"run {summary['run_id']}: {summary['offers_stored']} offer(s) stored")
    for message_id, note in summary["accepted"].items():
        typer.echo(f"  ok       {message_id}: {note}")
    for message_id, note in summary["rejected"].items():
        typer.echo(f"  {_err}    {message_id}: {note}")
    if summary["unverified_evidence"]:
        typer.echo(
            f"  {_warn} quotes that could not be found in the email: "
            + ", ".join(summary["unverified_evidence"])
        )
    if summary["needs_confirmation"]:
        typer.echo(f"  {summary['needs_confirmation']} offer(s) need confirmation")
    typer.echo(f"  {summary['coverage_note']}")


@app.command()
def status(
    config: Annotated[str | None, typer.Option()] = None,
    offline: Annotated[bool, typer.Option()] = False,
) -> None:
    """Show configuration and last-run status. Never prints a credential."""
    service = _service(config, offline)
    typer.echo(json.dumps(service.status(), ensure_ascii=False, indent=2))


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind address; loopback only.")] = "127.0.0.1",
    port: Annotated[int, typer.Option()] = 8765,
    config: Annotated[str | None, typer.Option()] = None,
    offline: Annotated[bool, typer.Option()] = False,
) -> None:
    """Serve the local web UI on loopback."""
    if host not in ("127.0.0.1", "::1", "localhost"):
        typer.echo(f"{_err} refusing to bind to {host}; v0.1 is loopback-only")
        raise typer.Exit(2)
    try:
        import uvicorn
    except ImportError:
        typer.echo(f"{_err} uvicorn is not installed")
        raise typer.Exit(2) from None

    from .web.app import create_app

    settings = Settings.build(config, offline=offline)
    service = WeeklyDealsService(settings, SystemClock(settings.app.report.timezone))
    typer.echo(f"serving on http://{host}:{port}  (local only)")
    uvicorn.run(create_app(service), host=host, port=port, log_level="info")


@app.command("mcp")
def mcp(
    config: Annotated[str | None, typer.Option()] = None,
    offline: Annotated[bool, typer.Option()] = False,
) -> None:
    """Expose Weekly Deals to an MCP host over stdio.

    This is the direction in which MCP is worth using here: other agents call
    Weekly Deals, rather than Weekly Deals calling a mail server through MCP.
    """
    from .mcp_server import run_stdio

    run_stdio(config=config, offline=offline)


def main() -> None:  # pragma: no cover - entry point
    try:
        app()
    except KeyboardInterrupt:
        typer.echo("\ninterrupted", err=True)
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
