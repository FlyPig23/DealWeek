# Contributing

MealDeals is a local-first project. Contributions should keep mailbox access,
model calls, and user decisions separate.

## Local setup

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -e ".[all,dev]"
```

Run the offline checks before opening a pull request:

```bash
TYPESAFE_API_KEY='' LLM_API_KEY='' ./.venv/bin/python -m pytest -q
./.venv/bin/ruff check src tests
```

The test suite uses synthetic mail. Do not include real email bodies, OAuth
tokens, API keys, or personal data in issues, fixtures, screenshots, or pull
requests. Add a small synthetic fixture when a parser or renderer needs a
regression case.

## Skill and mailbox changes

The packaged skill is `skills/mealdeals/SKILL.md`. Keep its two mailbox modes
explicit: an agent host may read Gmail through its connector, while a
self-hosted install uses the read-only Gmail API scope. Neither mode may send,
archive, label, delete, or open promotional links.

JEV and other cloud providers are optional. New provider code must document what
email data leaves the machine and must keep consent checks in the configuration
layer.
