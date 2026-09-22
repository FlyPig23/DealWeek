#!/usr/bin/env python3
"""Regenerate the standalone skill's schema and extraction instructions.

The schema in this directory is a copy, kept here so a host can read it without
importing the package. Run this after changing the extraction wire format, or
the copy drifts from what `weekly-deals ingest` will actually accept.
"""

import json
import pathlib
from importlib.resources import files

from weekly_deals.models.wire import json_schema

target = pathlib.Path(__file__).with_name("offer-draft.schema.json")
target.write_text(
    json.dumps(json_schema(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print(f"wrote {target}")

prompt = target.parent / "references" / "extract_offers_v1.txt"
prompt.parent.mkdir(exist_ok=True)
prompt.write_text(
    files("weekly_deals").joinpath("prompts/extract_offers_v1.txt").read_text(encoding="utf-8"),
    encoding="utf-8",
)
print(f"wrote {prompt}")
