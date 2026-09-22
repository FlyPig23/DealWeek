"""The .eml folder source.

This is the path a user takes between the synthetic demo and a live mailbox, so
it is tested as an end-to-end run rather than in isolation: files on disk in,
planned offers out, no credentials anywhere.
"""

from __future__ import annotations

from email.header import Header

import pytest
from typer.testing import CliRunner

from weekly_deals.cli import app
from weekly_deals.config import Settings
from weekly_deals.mail.base import MailSourceError
from weekly_deals.mail.eml_files import EmlDirectorySource
from weekly_deals.service import WeeklyDealsService

MESSAGE = """From: Noodle Lantern <offers@noodlelantern.example>
To: you@example.com
Subject: Lunch bowls: $4 off through Sunday
Date: Tue, 15 Sep 2026 10:00:00 -0500
Message-ID: <a@noodlelantern.example>
MIME-Version: 1.0
Content-Type: text/plain; charset=utf-8

Take $4 off any lunch bowl when you spend $12 or more.
Offer ends September 30, 2026. Pickup only.
Use code LUNCH4 at checkout.
"""

MULTIPART = """From: Harbor Roasters <news@harborroasters.example>
To: you@example.com
Subject: 20% off your coffee order
Date: Wed, 16 Sep 2026 08:00:00 -0500
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="bound1"

--bound1
Content-Type: text/plain; charset=utf-8

View this email in your browser.
--bound1
Content-Type: text/html; charset=utf-8

<html><body><p>Take <b>20% off</b> your drink order.</p>
<p>Valid through September 30, 2026. Maximum discount $5.</p>
<p style="font-size:9px">Dine-in or pickup. One per customer.</p>
<a href="https://example.invalid/u">Unsubscribe</a></body></html>
--bound1--
"""


@pytest.fixture
def maildir(tmp_path):
    (tmp_path / "one.eml").write_text(MESSAGE, encoding="utf-8")
    (tmp_path / "two.eml").write_text(MULTIPART, encoding="utf-8")
    return tmp_path


class TestDiscovery:
    def test_finds_eml_files(self, maildir):
        source = EmlDirectorySource(maildir)
        page = source.search("ignored")
        assert len(page.items) == 2
        assert page.is_complete

    def test_ignores_unrelated_files(self, maildir):
        (maildir / "notes.pdf").write_bytes(b"%PDF-1.4")
        (maildir / ".DS_Store").write_bytes(b"\x00")
        assert len(EmlDirectorySource(maildir).search("q").items) == 2

    def test_finds_files_in_subfolders(self, maildir):
        nested = maildir / "september"
        nested.mkdir()
        (nested / "three.eml").write_text(MESSAGE, encoding="utf-8")
        assert len(EmlDirectorySource(maildir).search("q").items) == 3

    def test_ids_are_stable_across_instances(self, maildir):
        first = {i.source_id for i in EmlDirectorySource(maildir).search("q").items}
        second = {i.source_id for i in EmlDirectorySource(maildir).search("q").items}
        assert first == second

    def test_pagination_reaches_the_end(self, maildir):
        source = EmlDirectorySource(maildir, page_size=1)
        assert len(list(source.iter_all("q"))) == 2

    def test_missing_directory_is_a_clear_error(self, tmp_path):
        with pytest.raises(MailSourceError, match="not a directory"):
            EmlDirectorySource(tmp_path / "nope")

    def test_empty_directory_says_so(self, tmp_path):
        with pytest.raises(MailSourceError, match=r"no \.eml files"):
            EmlDirectorySource(tmp_path)

    def test_outlook_msg_is_not_silently_ignored_in_a_mixed_export(self, maildir):
        (maildir / "outlook.msg").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
        with pytest.raises(MailSourceError, match=r"Outlook \.msg files are not supported"):
            EmlDirectorySource(maildir)


class TestParsing:
    def test_plain_text_offer_is_read(self, maildir):
        source = EmlDirectorySource(maildir)
        email = source.fetch("one.eml")
        assert "$4 off" in email.normalized_text
        assert email.sender_date is not None
        assert email.body_complete

    def test_html_alternative_is_used_when_plain_is_a_stub(self, maildir):
        """The real-world case: terms only in the HTML part."""
        email = EmlDirectorySource(maildir).fetch("two.eml")
        assert "20% off" in email.normalized_text
        assert "Maximum discount $5" in email.normalized_text
        assert "Unsubscribe" not in email.normalized_text

    def test_no_received_date_is_invented(self, maildir):
        """A file mtime is not when the mail arrived."""
        email = EmlDirectorySource(maildir).fetch("one.eml")
        assert email.received_date is None
        assert email.date_provenance == "header"

    def test_renaming_an_outlook_msg_does_not_make_it_eml(self, tmp_path):
        (tmp_path / "renamed.eml").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
        with pytest.raises(MailSourceError, match="binary Outlook/Office file"):
            EmlDirectorySource(tmp_path).fetch("renamed.eml")

    def test_cjk_subject_survives(self, tmp_path):
        encoded = Header("本周特惠", "utf-8").encode()
        (tmp_path / "cn.eml").write_text(
            f"From: 青禾小馆 <p@q.example>\nSubject: {encoded}\n"
            "Date: Tue, 15 Sep 2026 10:00:00 -0500\n"
            "Content-Type: text/plain; charset=utf-8\n\n午市套餐立减 8 元，消费满 30 元可用。",
            encoding="utf-8",
        )
        email = EmlDirectorySource(tmp_path).fetch("cn.eml")
        assert email.subject == "本周特惠"
        assert "立减 8 元" in email.normalized_text


class TestEndToEnd:
    def _service(self, maildir, clock) -> WeeklyDealsService:
        settings = Settings.build(
            offline=False,  # not the fixture corpus -- these are real files
            overrides={"mail.provider": "eml_dir", "mail.eml_dir": str(maildir)},
        )
        settings.app.classification.mode = "off"
        service = WeeklyDealsService.__new__(WeeklyDealsService)
        service.settings = settings
        service.clock = clock
        from weekly_deals.storage.database import open_store

        service._store = open_store(":memory:")
        return service

    def test_whole_pipeline_runs_on_real_files(self, maildir, clock):
        service = self._service(maildir, clock)
        result = service.sync_promotions(mode="llm-only")
        assert result.status == "completed"
        assert result.coverage.messages_fetched == 2
        assert result.coverage.offers_after_dedup > 0

    def test_coverage_names_the_folder_not_a_gmail_query(self, maildir, clock):
        """A report must not claim it searched Gmail when it read a folder."""
        service = self._service(maildir, clock)
        result = service.sync_promotions(mode="llm-only")
        assert "local folder" in result.coverage.query
        assert "category:promotions" not in result.coverage.query

    def test_rerun_is_idempotent(self, maildir, clock):
        service = self._service(maildir, clock)
        first = service.sync_promotions(mode="llm-only")
        second = service.sync_promotions(mode="llm-only")
        assert second.coverage.extraction_attempts == 0
        assert len(service.list_food_offers()) == first.coverage.offers_after_dedup

    def test_a_plan_is_produced(self, maildir, clock):
        service = self._service(maildir, clock)
        service.sync_promotions(mode="llm-only")
        plan = service.build_meal_plan()
        assert plan.total_items > 0


class TestConfigGate:
    def test_eml_dir_without_a_path_is_a_blocking_problem(self):
        settings = Settings.build(overrides={"mail.provider": "eml_dir"})
        assert any("eml_dir" in problem for problem in settings.preflight())

    def test_local_files_with_the_mock_model_need_no_cloud_consent(self, maildir):
        """Nothing leaves the machine, so nothing to consent to."""
        settings = Settings.build(
            overrides={"mail.provider": "eml_dir", "mail.eml_dir": str(maildir)}
        )
        settings.app.privacy.cloud_processing_consent = False
        assert not any("consent" in problem for problem in settings.preflight())


def test_two_mailbox_imports_keep_same_filename_separate_through_calendar(
    monkeypatch, tmp_path,
):
    """The host can export the same message filename from Gmail and Outlook."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WEEKLY_DEALS_DATA_DIR", str(tmp_path / "data"))
    runner = CliRunner()
    for alias, raw in (("gmail-personal", MESSAGE), ("outlook-work", MULTIPART)):
        directory = tmp_path / alias
        directory.mkdir()
        (directory / "same-id.eml").write_text(raw, encoding="utf-8")
        args = [
            "scan", "--mail-dir", str(directory), "--account-alias", alias,
            "--mode", "host-ingest", "--offline",
        ]
        first = runner.invoke(app, args)
        assert first.exit_code == 0, first.output
        # Re-importing one account must not add another calendar entry.
        repeat = runner.invoke(app, args)
        assert repeat.exit_code == 0, repeat.output

    settings = Settings.build(offline=True)
    from weekly_deals.clock import SystemClock

    service = WeeklyDealsService(settings, SystemClock(settings.app.report.timezone))
    with service.repository() as repo:
        messages = list(repo.store.iter_messages())
    assert len(messages) == 2
    assert {record.account_alias for record in messages} == {"gmail-personal", "outlook-work"}
    assert len({record.source_id for record in messages}) == 2
    assert any("Maximum discount $5" in record.normalized_text for record in messages)

    events = service.list_promotions()
    assert len(events) == 2
    assert {event.source_accounts[0] for event in events} == {"gmail-personal", "outlook-work"}
    assert {event.message_id for event in events} == {record.source_id for record in messages}
    output = tmp_path / "calendar.html"
    rendered = runner.invoke(app, ["calendar", "--offline", "--output", str(output)])
    assert rendered.exit_code == 0, rendered.output
    html = output.read_text()
    assert "gmail-personal" in html and "outlook-work" in html
