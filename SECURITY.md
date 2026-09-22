# Security

## Threat model

This application reads a person's mailbox and sends selected text to third-party
model APIs. The interesting threats are therefore: credential leakage, indirect
prompt injection from email content, over-broad mailbox access, and a local web
server that other web pages can reach.

## Indirect prompt injection

Promotional email is attacker-controlled input. Some of it will contain text
aimed at whatever assistant reads it. The defence here is structural, not
textual:

1. **The extractor has no capabilities.** No tools, no network client, no shell,
   no credentials, no mailbox handle. It receives text and returns JSON. There
   is nothing for an injected instruction to operate.
2. **Its output is not trusted.** Everything is re-validated deterministically
   in `offers/validate.py`. A value whose quoted evidence cannot be located
   verbatim in the normalized body is marked unverified and cannot reach a plan.
3. **No side effects downstream.** Promotional URLs are stored as data fields
   and never fetched — opening a "claim" link can itself be the action an
   attacker wants. `privacy.follow_promotion_links` defaults to `false`.

Delimiters and "ignore instructions in the email" wording are used as
defence in depth (`models/openai_extractor.py:build_user_payload`), but they are
mitigations, not the boundary. A detector model would not be a boundary either.

Regression coverage: `tests/unit/test_normalize.py::TestPromptInjectionIsData`,
and fixture `fx-012` runs through the full pipeline on every demo.

## Credentials

- Read from the environment or `.env` through `SecretStr`. Never in
  `config.yaml`, which is meant to be shareable.
- Never included in model prompts, reports, logs or the recorded model calls.
- The Gmail token file is written `0600` inside the data directory.
- The store holds no credential of any kind. The Gmail token lives in its own
  `0600` file, created with those permissions rather than relaxed afterwards.
- Provider error responses are never echoed into logs: they can contain the
  email text that was sent.
- `mealdeals status` and the MCP `get_status` tool are asserted by tests never to
  return anything matching `api_key`, `token`, `secret`, `bearer` or `password`.

## Mailbox access

- The only scope requested is `https://www.googleapis.com/auth/gmail.readonly`.
- **That scope grants read access to the entire mailbox at the OAuth layer.**
  The restriction to promotional mail is enforced by this application's queries.
  Do not describe it to users as "MealDeals can only see promotions".
- No write scope, ever: no sending, no labelling, no deleting.
- An OAuth app in Testing status expires refresh tokens after 7 days. That
  surfaces as `AuthRequired` ("re-authorise"), not as an empty offer list.

## Local web server

Binding to loopback is not a security boundary — any page in any browser on the
machine can issue requests to `127.0.0.1`. `web/app.py` therefore:

- refuses non-loopback `Host` headers and non-loopback `Origin` headers;
- requires a CSRF token on every state-changing request;
- sets a `HttpOnly`, `SameSite=Strict` session cookie;
- sends `Content-Security-Policy: default-src 'none'` with no image or script
  sources, `X-Frame-Options: DENY` and `Referrer-Policy: no-referrer`;
- exposes no OpenAPI or docs endpoints;
- refuses to bind to anything but loopback (`mealdeals serve` exits on a
  non-loopback host).

## Rendering email content

Promotional HTML is never re-emitted. The normalizer produces text for the
model; the renderer escapes everything and emits a self-contained page with no
remote images, scripts, iframes or event attributes. Opening a report cannot
contact a merchant's tracker.

Asserted by `tests/contract/test_pipeline.py::test_html_escapes_email_content_and_loads_nothing_remote`.

## MCP server boundary

`mealdeals mcp` exposes seven tools. The limits are enforced in code and
asserted in `tests/contract/test_mcp_server.py`:

- Read and plan freely; the only write is the user's own offer state.
- No tool accepts a file path, URL, query or SQL fragment.
- `sync_promotions` is capped at 200 messages per call.
- `sync_promotions` refuses to run unless `privacy.cloud_processing_consent` is
  set **locally**. A host cannot grant itself permission to spend the user's
  money.
- No tool returns a credential.

## Supply chain

Nine runtime dependencies; everything else is an opt-in extra. Each one is in
the path that sees email text or credentials, which is why the list is short and
why there is no LLM framework (`docs/DECISIONS.md` §1).

Pin versions with a lockfile before deploying. Re-run the contract tests after
any provider SDK upgrade.

## Reporting a vulnerability

Open a private security advisory on the repository. Please do not include real
email content, tokens or API keys in a report — a synthetic reproduction is
more useful and safer.
