# Privacy

## Local-first does not mean offline

This is the single most important thing to understand before a real run. Data
leaves your machine, and you should know exactly what and to whom.

```
Gmail
  └─► your machine (normalize, store)
        ├─► TypeSafe / JEV API      ── subject + normalized body, per email
        ├─► your chosen LLM API     ── subject + normalized body + metadata
        └─► local JSON files + reports ── stays here
```

- **TypeSafe (JEV)** receives the subject and normalized body of each email that
  gets classified, as the `state` of a typed question.
- **Your LLM provider** receives the subject, normalized body, sender, structured
  markup and completeness flags of each email that reaches extraction. If you
  set `LLM_BASE_URL`, that is who receives it — changing the URL changes the
  recipient.
- **Nothing else leaves.** No telemetry, no analytics, no crash reporting. The
  `privacy.telemetry` setting exists and defaults to `false`; there is currently
  no code that would send anything if it were `true`.

## Consent is explicit and separate

Authorising Gmail reads and agreeing to send email text to a cloud model are two
different decisions. `privacy.cloud_processing_consent` defaults to `false`, and
a scan against a real mailbox refuses to start without it.

It can only be set in your local config file. No CLI flag, API call or MCP tool
argument can grant it — including for an agent acting on your behalf.

## What is stored locally

In `$XDG_DATA_HOME/mealdeals` (or `MEALDEALS_DATA_DIR`), mode `0700`:

| Table | Contents |
| --- | --- |
| `messages`, `message_payloads` | Normalized email text and headers, for offers still live |
| `classifications`, `extractions` | Model outputs, so a re-run costs nothing |
| `offers`, `offer_sources` | Extracted offers and the evidence behind them |
| `offer_user_state` | Your own decisions — used, dismissed, planned |
| `runs`, `model_calls` | Scan coverage and cost accounting |

`model_calls` records provider, model, token counts, latency and retry count.
It does **not** record prompts, email content or credentials.

## Minimisation

`privacy.redact_personal_identifiers` (default `true`) is the switch for
stripping names, addresses and order numbers before text is sent to a provider.
Redaction must not destroy the evidence a condition depends on, so it is applied
conservatively; personalised promo codes can be placeholdered locally and filled
back in after extraction.

**Note for v0.1:** the setting and the policy are defined; the redaction
transform itself is not yet implemented. Until it is, assume the full normalized
body of each processed email reaches your chosen providers. This is stated here
rather than implied by an unimplemented flag.

## Deletion

Disconnecting an account and deleting local data are separate operations.

To delete everything: remove the data directory. It is plain JSON files and
nothing else -- there is no database, and therefore no write-ahead log or
journal holding a copy of your mail after the delete. You can also remove parts
of it: `store/messages/` is the stored email text, `store/offers.json` the
offers derived from it, `store/user_state.json` your own decisions.

Stored message text also expires on its own. `privacy.payload_retention_days`
(default 180) drops the text of any email older than that, measured from the
email's own date, unless it is still the evidence behind a live offer. What
remains is the body hash and the model's verdict, which is enough for a later
scan to skip the message without reading your mailbox again.

Any reports you exported are ordinary files wherever you wrote them; `reports/`
is git-ignored but not managed by the application.

Revoking the Gmail grant is done in your Google account settings and is
independent of the local data.

## Provider terms

Check both providers' data-handling terms before a real run, and re-check before
publishing anything built on this. Defaults here do not send real email to
training, public datasets or telemetry, and "the user supplied their own API
key" is not a substitute for telling them where their mail goes.

Google's Workspace user data policy places requirements on use, transfer and
disclosure, and restricts advertising use and general model training. Whether
restricted-scope verification applies depends on how you deploy: personal use,
shared OAuth app and hosted service are different situations, and "it's open
source and runs locally" is not a blanket exemption.

## What this project never does

- Send email, delete email, or change labels.
- Open promotional or claim links.
- Place orders or make payments.
- Write to your calendar.
- Install a scheduled task without being asked.
- Upload your email anywhere other than the two providers named above.
