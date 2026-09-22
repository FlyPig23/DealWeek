# DealWeek

**Weekly Deals** is the savings assistant in DealWeek. Its command and skill
are named `weekly-deals`; its Python package is `weekly_deals`.

Turns promotional email into a weekly savings plan you can check: which offers
are usable this week, which expire soon, and which still need confirmation —
with the original wording behind every claim. Food, shopping, travel, events,
and services stay in one calendar with small categories. An optional JEV pass
groups repeat reminders for the same promotion into one calendar entry while
keeping the source emails available.

Local-first, single user, Gmail read-only. Runs offline on synthetic data with
no API keys at all.

```bash
git clone https://github.com/FlyPig23/DealWeek.git
cd DealWeek
python3 -m venv .venv
source .venv/bin/activate
pip install .
weekly_deals_demo_dir="$(mktemp -d)"
WEEKLY_DEALS_DATA_DIR="$weekly_deals_demo_dir" weekly-deals scan --offline --mode llm-only
WEEKLY_DEALS_DATA_DIR="$weekly_deals_demo_dir" weekly-deals calendar --output ./savings-calendar.html
```

Open `savings-calendar.html` in your browser. This example scans synthetic
messages in an isolated temporary data directory and builds the calendar
without network access or credentials.
Every merchant in it is invented.

---

## What makes it different from a list of coupons

The hard part of this problem is not finding offers. It is not overstating them.

- **An unstated deadline is not "never expires".** It is `time_status: unknown`,
  and it cannot enter a plan.
- **A claim deadline is not a redemption deadline.** "Redeemable until the 30th,
  but claim it by Friday" is two dates, and losing the second one loses the
  offer.
- **A quoted term that is not in the email is not a term.** Every material value
  must be backed by a verbatim quote the validator can locate in the normalized
  body. If it cannot, the offer is downgraded.
- **A coupon that needs a bigger basket than you would have bought is not a
  saving.** If the minimum spend is above your per-meal budget, it is not
  recommended — the point is to spend less, not to use coupons.
- **"We didn't find anything" and "we couldn't finish" are different screens.**
  Every report states its coverage: messages matched, fetched, failed, images
  left unparsed, and whether the search actually reached the end of the window.
  A run capped with `--max-messages` says so; it does not report the part it saw
  as the whole.
- **Your decisions are yours.** Marking an offer used or dismissed writes to a
  separate table. A re-sync rewrites model facts and never touches it.

---

## Install

```bash
pip install -e .                 # core: offline demo, planner, reports
pip install -e ".[gmail]"        # + read-only Gmail access
pip install -e ".[llm]"          # + OpenAI-compatible extraction
pip install -e ".[web]"          # + local web UI
pip install -e ".[mcp]"          # + expose Weekly Deals to MCP hosts
pip install -e ".[all,dev]"      # everything, plus tests
```

Requires Python 3.11+.

### Install the skill

After installing the application, copy the skill into your agent's skill folder:

```bash
# Codex
mkdir -p ~/.codex/skills
cp -R skills/weekly-deals ~/.codex/skills/

# Or Claude Code
mkdir -p ~/.claude/skills
cp -R skills/weekly-deals ~/.claude/skills/
```

Start a new turn/session and ask the agent to use `weekly-deals`. If the agent
already has read access to Gmail, no separate Gmail OAuth setup is needed.

### Connect JEV once (optional)

Skill installation copies instructions; it does not open an API-key form. For
JEV classification and promotion deduplication, run this in your own terminal
after installing the CLI:

```bash
weekly-deals auth jev
```

The command prompts for **your own** [TypeSafe API key](https://console.typesafe.ai)
with hidden input, verifies it on a synthetic coupon, then asks whether to allow
email subjects/bodies to be sent to TypeSafe. It saves the key in
`~/.config/weekly-deals/.env` (or `$XDG_CONFIG_HOME/weekly-deals/.env`) with
owner-only file permissions. It works from any directory. No changes to
`SKILL.md`, no key in chat, and no additional Gmail setup are needed in agent-host mode.

Already have `TYPESAFE_API_KEY` in your shell or project `.env`? Use
`weekly-deals auth jev --from-env` to verify and save it to the same user location.
An invalid key is not saved. To replace a key or change JEV email permission,
run the command again. This permission covers only the official TypeSafe API;
another cloud extractor needs its own configuration and cloud-processing consent.

`weekly-deals status` shows whether a key and permission are configured.
`weekly-deals doctor --check-apis` checks live connectivity with synthetic text
and exits with an error if a provider fails. Neither reads your mailbox.
`jev_configured: true` means a key is present, not that it has just been verified.

After setup, ask the host to use JEV with `weekly-deals`. It uses
`scan --mail-dir <dir> --mode host-ingest` without `--offline`.
JEV records promotion probabilities and category judgments. Run `weekly-deals
dedupe` after the scan to compare likely repeats, then generate the HTML calendar.
Both JEV stages reuse the same API key and email-processing permission; no extra
key is needed. Default `observe` mode keeps all messages; automatic rejection
(`gate`) needs a recorded evaluation.
The calendar's display categories currently come from local rules.
Already completed, unchanged messages are reused; connecting JEV does not
retrospectively reprocess those messages. New and pending mail uses the configured
classifier. Check the scan's classification count, not just its provider label.

Without JEV, explicitly choose the host-only path with `--offline`. The skill
explains this choice on first use rather than silently bypassing a requested JEV run.

### Merge repeated promotions

After fetching email through your agent's Gmail connector:

```bash
weekly-deals scan --mail-dir <exported-emails> --mode host-ingest
weekly-deals dedupe
weekly-deals calendar --output ./savings-calendar.html
```

`dedupe` asks JEV whether candidate emails describe the same promotion, including
reminders with different wording. The calendar then shows one entry per matched
group with its source message IDs. All original emails remain stored; nothing is
deleted or changed in Gmail. A shared merchant alone is not enough to merge two
offers. Uncertain matches and failed requests leave the messages separate.
Likely offers with upcoming deadlines are checked first; messages already scored
below the promotion-candidate threshold stay in the original-mail view. JEV
receives readable promotional text rather than email CSS and tracking URLs.

If grouped sources disagree on dates or terms, the entry is marked for review
and uses the earliest known deadline as a reminder, without treating the most
generous terms as confirmed. Unknown dates stay unknown. Review the reported
coverage and cost: deduplication uses the existing `runtime.per_run_budget_usd`
setting (default $1 per run), and reaching the budget can leave work unfinished.
Large mailboxes can use `weekly-deals dedupe --max-comparisons 10000` to raise the
default 5,000-comparison cap while retaining the dollar budget and cached judgments.

To inspect every original entry, use `weekly-deals calendar --all-messages`;
the same switch works with `--json`. Rendering is offline and free. The `dedupe`
step calls JEV and is not available as a simulated offline result.

### Upgrading from MealDeals

Reinstall the application and replace the old `mealdeals` skill folder with
`skills/weekly-deals`. Update scripts and MCP configurations to call
`weekly-deals`, and Python imports to use `weekly_deals`.

The environment prefix is now `WEEKLY_DEALS_`. Existing `MEALDEALS_DATA_DIR`,
`MEALDEALS_EML_DIR`, `MEALDEALS_TIMEZONE`, and `MEALDEALS_LANGUAGE` still work;
the corresponding new name takes priority when both are supplied.
New installations use `~/.local/share/weekly-deals` (or the equivalent under
`XDG_DATA_HOME`). If that directory does not exist but the old `mealdeals`
data directory does, it is reused so saved mail, decisions and OAuth tokens
remain available. No private data is moved automatically.

## Commands

```bash
weekly-deals demo                   # offline, synthetic, no keys
weekly-deals doctor                 # check configuration
weekly-deals doctor --check-apis    # send one synthetic probe per provider
weekly-deals auth jev               # hidden key entry, verification, private user config
weekly-deals auth gmail             # read-only OAuth, token stored locally

weekly-deals scan --mail-dir ~/Desktop/test-emails --offline   # real files, no keys
weekly-deals scan --mode llm-only --max-messages 30
weekly-deals scan --mode jev-observe --lookback-days 90
weekly-deals scan --mode jev-gate           # only after an evaluation is recorded
weekly-deals dedupe                 # JEV: group repeated promotions after a scan

weekly-deals plan                   # show the plan in the terminal
weekly-deals offers                 # list stored offers and their state
weekly-deals calendar               # HTML, with saved duplicate groups merged
weekly-deals calendar --json        # machine-readable grouped events
weekly-deals calendar --all-messages # inspect every original Promotions entry
weekly-deals savings                # same HTML calendar under another command name
weekly-deals mark <offer-id> --status used
weekly-deals report --format html --output ./report.html
weekly-deals serve                  # local web UI on 127.0.0.1
weekly-deals mcp                    # MCP server over stdio
```

## First real run

The order matters, and each step is cheap to undo. Steps 1-2 need no account,
no key and no network.

1. `weekly-deals demo` — confirm the pipeline works end to end on synthetic mail.

2. **Test on real email without giving anything away.** Export a dozen
   promotional messages from your mail client (in Gmail: open a message → the
   three-dot menu → "Download message"; in Apple Mail, drag them to a folder),
   then:

   ```bash
   weekly-deals scan --mail-dir ~/Desktop/test-emails --offline
   weekly-deals plan --offline
   weekly-deals calendar --offline
   ```

   This runs the whole pipeline over real vendor HTML, real nested MIME, real
   encodings — with no OAuth, no API key, and nothing leaving the machine. It is
   the fastest way to find out whether the normalizer copes with the mail *you*
   actually get, which is the part the synthetic corpus tests least honestly.
   Check the parsed dates, amounts and conditions against the originals before
   going further.

3. `weekly-deals auth jev` if using JEV, then `weekly-deals doctor --check-apis` — confirm your keys and model names, using
   synthetic text only. Your mailbox is not touched.
4. `weekly-deals auth gmail` — read-only authorisation.
5. For a cloud extractor, set `privacy.cloud_processing_consent: true` in
   `config.yaml` and pass `--config config.yaml` to scans. JEV-only use can grant
   its narrower permission through `weekly-deals auth jev`. Authorising
   Gmail reads and agreeing to send email text to a cloud model are two separate
   decisions, and the application will not do the second without this. It covers
   every remote model that sees your mail, JEV included — running the classifier
   alone still sends each subject and body to TypeSafe.

   If you set `runtime.per_run_budget_usd`, also set
   `llm_price_input_usd_per_mtok` and `llm_price_output_usd_per_mtok` for your
   model. A cap can only be applied to spend that can be measured, so a run with
   a cap and no prices refuses to start rather than pretending to be capped.
6. `weekly-deals scan --mode llm-only --max-messages 30` — a small batch. Read the
   output and check it against the real emails. The default Gmail query is
   `category:promotions`, so archived Promotions mail is included; the app does
   not restrict this step to `in:inbox`.
7. Fix whatever is wrong, add a test for it, then widen to 90 days.
8. `weekly-deals scan --mode jev-observe` — the classifier runs but discards
   nothing, so you can measure what it would have filtered.
9. Only once you have an evaluation record showing acceptable recall, switch to
   `jev-gate`.

## Choose how Gmail is connected

There are two supported paths. Choose one; they do not need to be configured
at the same time.

**Agent-host mode (recommended for Codex or Claude Code).** The host already
has a Gmail connector, so the user authorises the host to read the mailbox and
asks it to use the Weekly Deals skill. No Google Cloud OAuth client, Gmail token,
JEV key, or LLM key is required for the host-only (`--offline`) path. JEV can be
added with `weekly-deals auth jev` as described above. The host hands Weekly Deals the
messages it fetched, and Weekly Deals performs local normalisation, validation,
calendar generation, and reporting. The host must preserve the message body and
must not modify the mailbox.

For example, ask the host:

> Use the Weekly Deals skill to read my last 30 days of Gmail Promotions, use
> JEV to classify and deduplicate them, and generate the HTML savings calendar.
> Read only; do not send, archive, label,
> or delete anything.

**Self-hosted Gmail API mode.** Use this when Weekly Deals itself should access
Gmail from a terminal or server:

~~~bash
cp .env.example .env
# edit .env:
#   MAIL_PROVIDER=gmail_api
#   GMAIL_CLIENT_SECRET_PATH=/absolute/path/client_secret.json
./.venv/bin/weekly-deals auth gmail
./.venv/bin/weekly-deals scan --mode llm-only
./.venv/bin/weekly-deals calendar
~~~

For local processing, use `--mode llm-only` (or set `classification.mode: "off"`
in a file passed with `--config`) and
leave LLM_PROVIDER=mock. This uses Gmail only for reading and does not send
email text to a cloud model. The mock extractor is deterministic and is mainly
intended for testing.

### Is JEV required?

No. JEV provides optional semantic classification and promotion deduplication in
both agent-host and self-hosted runs. Run `weekly-deals auth jev` to configure it. The default model is
`jev-latest`, using the [official TypeSafe HTTP API](https://docs.typesafe.ai/api).
Agent-host scans use `--offline` when JEV is intentionally omitted.
Subjects and normalised bodies sent to JEV leave the machine; the key is never
committed to the repository.

### If the command says `No module named 'weekly_deals'`

On macOS with iCloud Drive's "Desktop & Documents" sync enabled, iCloud sets the
hidden flag on files it treats as internal — including the `.pth` file an
editable install (`pip install -e .`) leaves in `site-packages`. Python's `site`
module skips hidden `.pth` files, so the package silently disappears from the
import path.

Clearing the flag by hand does not hold — iCloud sets it again within minutes.
Keep the virtualenv outside the synced folders instead:

```bash
python3 -m venv ~/.venvs/weekly-deals
~/.venvs/weekly-deals/bin/pip install "/path/to/DealWeek[gmail,llm,web]"
# ...then link it somewhere already on your PATH. Check first:
#   echo $PATH | tr ':' '\n' | grep -E 'local/bin|homebrew/bin'
ln -sf ~/.venvs/weekly-deals/bin/weekly-deals /opt/homebrew/bin/weekly-deals
```

`~/.local/bin` is the conventional target, but it is **not** on the default macOS
PATH — add it to `~/.zshrc` first if you use it, or link somewhere that already
is.

A non-editable install puts real files in `site-packages` and needs no `.pth` at
all, so it is immune regardless.

The same applies to your data: keep `WEEKLY_DEALS_DATA_DIR` off the Desktop and out
of Documents. The default (`~/.local/share/weekly-deals`) is already outside them —
pointing it at a synced folder would upload your email text to iCloud.

## Configuration

Business settings are loaded with `--config config.yaml` (see `config.example.yaml`);
no configuration file is assumed when the flag is omitted. Credentials
live in private dotenv files or the environment (see `.env.example`) and are never
written to reports, logs or model prompts.

Credential precedence, highest first: shell environment → current-directory
`.env` → user `~/.config/weekly-deals/.env`. Even an empty project key overrides
the user key; remove unused entries rather than leaving blank placeholders.
Business settings: defaults → explicit config file → environment → CLI flags.

## How it works

```
Gmail (read-only)  ──►  normalize (MIME/HTML/JSON-LD)
                              │
                     JEV classifier  ──► observe | gate routing
                              │
                        LLM extractor  ──► OfferDraft[] + evidence
                              │
            validate ─ temporal ─ eligibility ─ deduplicate   (deterministic)
                              │
                     JSON files  ──►  savings calendar / rule planner  ──►  report
```

Plain Python controls the sequence. No model decides what happens next, so the
failure modes are enumerable and the whole thing is testable offline.

The generic Promotions calendar also has a separate post-scan path:
`scan → dedupe (JEV) → calendar`. It groups repeated campaigns without requiring
meal-offer extraction. This is separate from the deterministic offer-level
deduplication shown above; neither stage deletes source email.

`docs/DECISIONS.md` explains the choices people ask about most: why there is no
LLM framework here, why MCP is used to *expose* this application rather than to
read mail with it, and why the storage is a directory of JSON files rather than
a database.

## For agent hosts

```bash
weekly-deals mcp
```

Exposes seven tools: `get_status`, `sync_promotions`, `list_food_offers`,
`get_offer`, `build_meal_plan`, `render_report`, `set_offer_state`. They call
the same service layer the CLI does.

The boundary is enforced, not advisory: read and plan freely, write only the
user's own offer state, no file paths or URLs as arguments, a hard cap on
messages per sync, and no way for a host to grant itself consent to spend money
on cloud model calls.

Example, for a client that launches servers over stdio:

```json
{
  "mcpServers": {
    "weekly-deals": { "command": "weekly-deals", "args": ["mcp"] }
  }
}
```

## Development

```bash
pip install -e ".[all,dev]"
pytest                    # offline regression suite; no network or mailbox
ruff check src tests
```

CI needs no secrets. The clock is injected everywhere, so time-dependent
behaviour is tested at frozen instants rather than by waiting.

## Status and limits

v0.1 is a working skeleton with a complete offline path. Known limits, stated
rather than hidden:

- Offers inside images are flagged `needs_visual`, not parsed. They are counted
  in coverage, never silently dropped.
- One Gmail account; default scope is 90 days of `category:promotions`, across
  food, shopping, travel, events, and services. The calendar indexes every
  matched message; dates it cannot prove remain visible as `unknown` and are
  marked for review.
- `gmail.readonly` grants read access to the whole mailbox at the OAuth layer.
  The restriction to promotional mail is enforced by this application's queries,
  not by Google.
- No incremental history sync yet; each scan re-searches the window and skips
  unchanged messages by body hash. Expiry is recomputed locally on every read,
  so a stored offer never reports itself as live after its date has passed.
- Offers inside attached documents (a PDF flyer) are flagged `needs_visual`, not
  read. They are counted, never silently treated as fully parsed.
- One email per model call, deliberately — see `docs/DECISIONS.md` §2b. Fetching
  is parallel where the source allows it; the paid stages are still serial
  because parallelising them races the budget cap.
- JEV `gate` mode is refused until you record an evaluation. `observe` is the
  default for a reason.
- The extraction quality of the offline mock is deliberately modest — it is a
  deterministic rule engine for testing the pipeline, not a language model.

## Your data

Everything lives in one directory — `$WEEKLY_DEALS_DATA_DIR`, or the platform
default — as plain JSON:

```
store/
  messages/<key>.json    one email: its text and its model results
  offers.json            the offers derived from them
  promotions.json        the generic savings calendar events
  user_state.json        used / dismissed / planned -- your decisions
  duplicates.json        near-matches waiting for you to settle
  runs.json              recent scans, with what they cost
  plans/<id>.json        plan snapshots
```

You can read it, grep it, correct it by hand, or delete it with `rm -r`. There
is no database file, and no write-ahead log keeping a copy of your mail after
you delete it.

`privacy.payload_retention_days` (default 180) drops stored message text once
it is that old, measured from the email's own date. Text still backing a live
offer is kept regardless — the validator's quotes have to stay checkable — and
a pruned record keeps its hash, so a re-scan still skips it without re-reading
your mailbox.

## Licence

MIT. See `SECURITY.md` and `PRIVACY.md` for the threat model and what leaves
your machine.
