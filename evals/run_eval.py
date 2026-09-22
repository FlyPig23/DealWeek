#!/usr/bin/env python3
"""Compare the three routing pipelines on a labelled set.

    python evals/run_eval.py --dataset evals/dataset.example.jsonl --pipeline all

Pipelines:
    llm-only   every candidate goes to the extractor (the product baseline)
    rule+llm   a keyword prefilter, to test whether JEV earns its place at all
    jev+llm    the classifier gates, so recall and cost can be traded off

Two things this script deliberately refuses to do:

* Report a headline recall figure from a handful of positives. With few
  examples the confidence interval is wider than the difference you are trying
  to measure, and it says so instead of printing a number that looks decisive.
* Split rows randomly. Reminder emails for one campaign must land in the same
  fold, or the test set is contaminated by its own development set. Rows carry a
  ``group`` for this.

Without ``TYPESAFE_API_KEY`` the JEV pipeline uses the offline mock classifier,
so the harness itself can be tested with no key and no spend.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mealdeals.models.jev import route_for
from mealdeals.models.mock import MockClassifier
from mealdeals.schemas import NormalizedEmail, Route

KEYWORDS = (
    "off", "free", "coupon", "bogo", "discount", "deal", "reward", "save",
    "优惠", "立减", "券", "满减",
)


@dataclass
class Row:
    id: str
    group: str
    subject: str
    body: str
    contains_food_offer: bool
    offer_count: int = 0
    body_complete: bool = True
    has_unparsed_visuals: bool = False
    note: str = ""

    def to_email(self) -> NormalizedEmail:
        return NormalizedEmail(
            source_id=self.id,
            subject=self.subject,
            normalized_text=f"Subject: {self.subject}\n\n{self.body}",
            body_complete=self.body_complete,
            has_unparsed_visuals=self.has_unparsed_visuals,
        )


@dataclass
class Outcome:
    true_positive: int = 0
    false_negative: int = 0
    true_negative: int = 0
    false_positive: int = 0
    sent_to_extractor: int = 0
    total: int = 0
    cost_usd: float = 0.0
    cost_known: bool = True
    missed: list[str] = field(default_factory=list)

    @property
    def recall(self) -> float | None:
        positives = self.true_positive + self.false_negative
        return self.true_positive / positives if positives else None

    @property
    def negative_pass_rate(self) -> float | None:
        negatives = self.true_negative + self.false_positive
        return self.false_positive / negatives if negatives else None

    @property
    def extractor_load(self) -> float:
        return self.sent_to_extractor / self.total if self.total else 0.0


def wilson_interval(successes: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval: behaves sensibly when recall is 1.0 on few samples."""
    if trials == 0:
        return (0.0, 1.0)
    phat = successes / trials
    denominator = 1 + z**2 / trials
    centre = (phat + z**2 / (2 * trials)) / denominator
    margin = z * math.sqrt((phat * (1 - phat) + z**2 / (4 * trials)) / trials) / denominator
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def load(path: Path) -> list[Row]:
    rows: list[Row] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            rows.append(Row(**json.loads(line)))
        except (json.JSONDecodeError, TypeError) as exc:
            print(f"{path}:{number}: skipped ({exc})", file=sys.stderr)
    return rows


def split_by_group(rows: list[Row], holdout: float = 0.5) -> tuple[list[Row], list[Row]]:
    """Group-aware split. Every row of one campaign stays on one side."""
    groups: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        groups[row.group].append(row)
    names = sorted(groups)
    cut = max(1, int(len(names) * (1 - holdout)))
    dev = [r for name in names[:cut] for r in groups[name]]
    test = [r for name in names[cut:] for r in groups[name]]
    return dev, test


def run_llm_only(rows: list[Row]) -> Outcome:
    outcome = Outcome(total=len(rows))
    for row in rows:
        outcome.sent_to_extractor += 1
        if row.contains_food_offer:
            outcome.true_positive += 1
        else:
            outcome.false_positive += 1
    return outcome


def run_rule_plus_llm(rows: list[Row]) -> Outcome:
    outcome = Outcome(total=len(rows))
    for row in rows:
        text = f"{row.subject} {row.body}".lower()
        passed = any(keyword in text for keyword in KEYWORDS)
        if passed:
            outcome.sent_to_extractor += 1
        if row.contains_food_offer:
            if passed:
                outcome.true_positive += 1
            else:
                outcome.false_negative += 1
                outcome.missed.append(row.id)
        else:
            outcome.false_positive += 1 if passed else 0
            outcome.true_negative += 0 if passed else 1
    return outcome


def run_jev_plus_llm(rows: list[Row], classifier, reject_below: float, accept_above: float) -> Outcome:
    outcome = Outcome(total=len(rows))
    for row in rows:
        email = row.to_email()
        result = classifier.classify(email)
        usage = result.meta.usage
        if usage.cost_known and usage.estimated_cost_usd is not None:
            outcome.cost_usd += usage.estimated_cost_usd
        else:
            outcome.cost_known = False

        route = route_for(
            result, email, mode="gate", reject_below=reject_below, accept_above=accept_above
        )
        passed = route is not Route.PROVISIONAL_REJECT
        if passed:
            outcome.sent_to_extractor += 1
        if row.contains_food_offer:
            if passed:
                outcome.true_positive += 1
            else:
                outcome.false_negative += 1
                outcome.missed.append(row.id)
        else:
            outcome.false_positive += 1 if passed else 0
            outcome.true_negative += 0 if passed else 1
    return outcome


def report(name: str, outcome: Outcome, target: float) -> None:
    print(f"\n--- {name} ---")
    print(f"  messages:              {outcome.total}")
    print(f"  sent to extractor:     {outcome.sent_to_extractor} "
          f"({outcome.extractor_load:.0%} of the corpus)")

    positives = outcome.true_positive + outcome.false_negative
    if outcome.recall is None:
        print("  food-offer recall:     n/a (no positive examples)")
    else:
        low, high = wilson_interval(outcome.true_positive, positives)
        print(f"  food-offer recall:     {outcome.recall:.1%} "
              f"(TP={outcome.true_positive} FN={outcome.false_negative}, n={positives})")
        print(f"  95% interval:          [{low:.1%}, {high:.1%}]")
        if positives < 50:
            print(f"  ! only {positives} positives. Too few to claim a "
                  f"{target:.0%} recall target -- stay in observe mode.")
        elif low < target:
            print(f"  ! the interval's lower bound is below the {target:.0%} target. "
                  "Gate mode is not justified yet.")
        else:
            print(f"  + lower bound clears the {target:.0%} target.")

    if outcome.negative_pass_rate is not None:
        print(f"  negatives passed on:   {outcome.negative_pass_rate:.1%} "
              "(a cost figure, not a quality one)")
    if outcome.cost_usd or not outcome.cost_known:
        cost = f"${outcome.cost_usd:.6f}" if outcome.cost_known else "unknown (no usage reported)"
        print(f"  classifier cost:       {cost}")
    if outcome.missed:
        print(f"  MISSED (these are the ones that matter): {', '.join(outcome.missed[:10])}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="evals/dataset.example.jsonl")
    parser.add_argument(
        "--pipeline", default="all", choices=["all", "llm-only", "rule+llm", "jev+llm"]
    )
    parser.add_argument("--reject-below", type=float, default=0.05)
    parser.add_argument("--accept-above", type=float, default=0.70)
    parser.add_argument("--recall-target", type=float, default=0.98)
    parser.add_argument(
        "--split", action="store_true", help="Report dev and test folds separately."
    )
    args = parser.parse_args()

    path = Path(args.dataset)
    if not path.exists():
        print(f"dataset not found: {path}", file=sys.stderr)
        return 2

    rows = load(path)
    if not rows:
        print("dataset is empty", file=sys.stderr)
        return 2

    classifier = MockClassifier()
    if os.environ.get("TYPESAFE_API_KEY"):
        from mealdeals.models.jev import JevClassifier, load_questions

        classifier = JevClassifier(
            os.environ["TYPESAFE_API_KEY"],
            model=os.environ.get("JEV_MODEL", "jev-1.13.0"),
            questions=load_questions()["questions"],
        )
        print("using the live JEV classifier")
    else:
        print("TYPESAFE_API_KEY not set -- using the offline mock classifier")
        print("(the harness is exercised; the numbers are not a JEV evaluation)")

    folds = [("all", rows)]
    if args.split:
        dev, test = split_by_group(rows)
        folds = [("dev", dev), ("test", test)]
        print(f"\ngroup-aware split: {len(dev)} dev / {len(test)} test rows")
        print("thresholds are chosen on dev and reported on test; never the reverse.")

    for fold_name, fold in folds:
        print(f"\n{'=' * 60}\nfold: {fold_name}  ({len(fold)} rows, "
              f"{sum(1 for r in fold if r.contains_food_offer)} positive)\n{'=' * 60}")
        if args.pipeline in ("all", "llm-only"):
            report("llm-only", run_llm_only(fold), args.recall_target)
        if args.pipeline in ("all", "rule+llm"):
            report("rule+llm", run_rule_plus_llm(fold), args.recall_target)
        if args.pipeline in ("all", "jev+llm"):
            report(
                "jev+llm (gate)",
                run_jev_plus_llm(fold, classifier, args.reject_below, args.accept_above),
                args.recall_target,
            )

    print(
        "\nReminder: latency and total cost must be compared cold-cache against "
        "cold-cache. A cached JEV run against an uncached baseline is not a result."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
