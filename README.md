# DealWeek

The public project is called **DealWeek**. The `mealdeals` Python package,
CLI, and skill name are kept for compatibility with existing local installs.

Turns promotional email into a weekly savings plan you can check: which offers
are usable this week, which expire soon, and which still need confirmation —
with the original wording behind every claim. Food, shopping, travel, events,
and services stay in one calendar with small categories.

Local-first, single user, Gmail read-only. Runs offline on synthetic data with
no API keys at all.

```bash
pip install -e .
mealdeals demo --output ./demo-report.html
```

That runs the whole pipeline — fetch, classify, extract, validate, deduplicate,
plan, render — against a synthetic corpus, with no network access and no
credentials. Every merchant in it is invented.

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
pip install -e ".[mcp]"          # + expose MealDeals to MCP hosts
pip install -e ".[all,dev]"      # everything, plus tests
```

Requires Python 3.11+.

## Commands

```bash
mealdeals demo                   # offline, synthetic, no keys
mealdeals doctor                 # check configuration
mealdeals doctor --check-apis    # send one synthetic probe per provider
mealdeals auth gmail             # read-only OAuth, token stored locally

mealdeals scan --mail-dir ~/Desktop/test-emails --offline   # real files, no keys
mealdeals scan --mode llm-only --max-messages 30
mealdeals scan --mode jev-observe --lookback-days 90
mealdeals scan --mode jev-gate           # only after an evaluation is recorded

mealdeals plan                   # show the plan in the terminal
mealdeals offers                 # list stored offers and their state
mealdeals calendar               # write ./savings-calendar.html
mealdeals calendar --json        # print machine-readable events
mealdeals savings                # same HTML calendar under another command name
mealdeals mark <offer-id> --status used
mealdeals report --format html --output ./report.html
mealdeals serve                  # local web UI on 127.0.0.1
mealdeals mcp                    # MCP server over stdio
```

## First real run

The order matters, and each step is cheap to undo. Steps 1-2 need no account,
no key and no network.

1. `mealdeals demo` — confirm the pipeline works end to end on synthetic mail.

2. **Test on real email without giving anything away.** Export a dozen
   promotional messages from your mail client (in Gmail: open a message → the
   three-dot menu → "Download message"; in Apple Mail, drag them to a folder),
   then:

   ```bash
   mealdeals scan --mail-dir ~/Desktop/test-emails --offline
   mealdeals plan --offline
   mealdeals calendar --offline
   ```

   This runs the whole pipeline over real vendor HTML, real nested MIME, real
   encodings — with no OAuth, no API key, and nothing leaving the machine. It is
   the fastest way to find out whether the normalizer copes with the mail *you*
   actually get, which is the part the synthetic corpus tests least honestly.
   Check the parsed dates, amounts and conditions against the originals before
   going further.

3. `mealdeals doctor --check-apis` — confirm your keys and model names, using
   synthetic text only. Your mailbox is not touched.
4. `mealdeals auth gmail` — read-only authorisation.
5. Set `privacy.cloud_processing_consent: true` in `config.yaml`. Authorising
   Gmail reads and agreeing to send email text to a cloud model are two separate
   decisions, and the application will not do the second without this. It covers
   every remote model that sees your mail, JEV included — running the classifier
   alone still sends each subject and body to TypeSafe.

   If you set `runtime.per_run_budget_usd`, also set
   `llm_price_input_usd_per_mtok` and `llm_price_output_usd_per_mtok` for your
   model. A cap can only be applied to spend that can be measured, so a run with
   a cap and no prices refuses to start rather than pretending to be capped.
6. `mealdeals scan --mode llm-only --max-messages 30` — a small batch. Read the
   output and check it against the real emails. The default Gmail query is
   `category:promotions`, so archived Promotions mail is included; the app does
   not restrict this step to `in:inbox`.
7. Fix whatever is wrong, add a test for it, then widen to 90 days.
8. `mealdeals scan --mode jev-observe` — the classifier runs but discards
   nothing, so you can measure what it would have filtered.
9. Only once you have an evaluation record showing acceptable recall, switch to
   `jev-gate`.

## Choose how Gmail is connected

There are two supported paths. Choose one; they do not need to be configured
at the same time.

**Agent-host mode (recommended for Codex or Claude Code).** The host already
has a Gmail connector, so the user authorises the host to read the mailbox and
asks it to use the MealDeals skill. No Google Cloud OAuth client, Gmail token,
JEV key, or LLM key is required for this path. The host hands MealDeals the
messages it fetched, and MealDeals performs local normalisation, validation,
calendar generation, and reporting. The host must preserve the message body and
must not modify the mailbox.

For example, ask the host:

> Use the MealDeals skill to read my last 90 days of Gmail Promotions and
> generate the weekly savings calendar. Read only; do not send, archive, label,
> or delete anything.

**Self-hosted Gmail API mode.** Use this when MealDeals itself should access
Gmail from a terminal or server:

~~~bash
cp .env.example .env
# edit .env:
#   MAIL_PROVIDER=gmail_api
#   GMAIL_CLIENT_SECRET_PATH=/absolute/path/client_secret.json
./.venv/bin/mealdeals auth gmail
./.venv/bin/mealdeals scan --mode llm-only
./.venv/bin/mealdeals calendar
~~~

For a completely local run, set classification.mode: off in config.yaml and
leave LLM_PROVIDER=mock. This uses Gmail only for reading and does not send
email text to a cloud model. The mock extractor is deterministic and is mainly
intended for testing.

### Is JEV required?

No. JEV is an optional semantic classifier used by self-hosted observe or gate
runs. Agent-host mode does not need it, and a self-hosted local run can set
classification.mode: off. If you enable JEV, add TYPESAFE_API_KEY to .env, use
JEV_MODEL=jev-latest, and explicitly set privacy.cloud_processing_consent: true.
Subjects and normalised bodies sent to JEV leave the machine; the key is never
committed to the repository.

### If the command says `No module named 'mealdeals'`

On macOS with iCloud Drive's "Desktop & Documents" sync enabled, iCloud sets the
hidden flag on files it treats as internal — including the `.pth` file an
editable install (`pip install -e .`) leaves in `site-packages`. Python's `site`
module skips hidden `.pth` files, so the package silently disappears from the
import path.

Clearing the flag by hand does not hold — iCloud sets it again within minutes.
Keep the virtualenv outside the synced folders instead:

```bash
python3 -m venv ~/.venvs/mealdeals
~/.venvs/mealdeals/bin/pip install "/path/to/mealdeals[gmail,llm,web]"
# ...then link it somewhere already on your PATH. Check first:
#   echo $PATH | tr ':' '\n' | grep -E 'local/bin|homebrew/bin'
ln -sf ~/.venvs/mealdeals/bin/mealdeals /opt/homebrew/bin/mealdeals
```

`~/.local/bin` is the conventional target, but it is **not** on the default macOS
PATH — add it to `~/.zshrc` first if you use it, or link somewhere that already
is.

A non-editable install puts real files in `site-packages` and needs no `.pth` at
all, so it is immune regardless.

The same applies to your data: keep `MEALDEALS_DATA_DIR` off the Desktop and out
of Documents. The default (`~/.local/share/mealdeals`) is already outside them —
pointing it at a synced folder would upload your email text to iCloud.

## Configuration

Business settings live in `config.yaml` (see `config.example.yaml`). Credentials
live only in the environment or `.env` (see `.env.example`) and are never
written to reports, logs or model prompts.

Precedence: defaults → `config.yaml` → environment → CLI flags.

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

`docs/DECISIONS.md` explains the choices people ask about most: why there is no
LLM framework here, why MCP is used to *expose* this application rather than to
read mail with it, and why the storage is a directory of JSON files rather than
a database.

## For agent hosts

```bash
mealdeals mcp
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
    "mealdeals": { "command": "mealdeals", "args": ["mcp"] }
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

Everything lives in one directory — `$MEALDEALS_DATA_DIR`, or the platform
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
