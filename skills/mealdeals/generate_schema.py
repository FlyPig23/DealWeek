#!/usr/bin/env python3
"""Regenerate offer-draft.schema.json from the code.

The schema in this directory is a copy, kept here so a host can read it without
importing the package. Run this after changing the extraction wire format, or
the copy drifts from what `mealdeals ingest` will actually accept.
"""

import json
import pathlib

from mealdeals.models.wire import json_schema

target = pathlib.Path(__file__).with_name("offer-draft.schema.json")
target.write_text(
    json.dumps(json_schema(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print(f"wrote {target}")
