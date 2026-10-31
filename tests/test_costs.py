from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from gemini_media_mcp.config import Settings
from gemini_media_mcp.costs import (
    BudgetExceededError,
    CostEntry,
    CostLedger,
    CostTotals,
    check_budget,
    estimate_image_cost,
    estimate_video_cost,
)

from .conftest import TZ


def test_image_estimate(settings: Settings) -> None:
    lite = settings.catalog.models["gemini-3.1-flash-lite-image"]
    cost = estimate_image_cost(lite, "1K", "a cat", 0)
    assert cost == pytest.approx(0.0336, abs=1e-5)
    pro = settings.catalog.models["gemini-3-pro-image"]
    with_refs = estimate_image_cost(pro, "4K", "x" * 300, 3)
    assert with_refs == pytest.approx(0.24 + (100 + 3 * 1120) * 2.0 / 1e6)


def test_video_estimate_assumes_ten_seconds(settings: Settings) -> None:
    omni = settings.catalog.models["gemini-omni-1.1-flash"]
    assert estimate_video_cost(omni, "720p", "", 0) == pytest.approx(1.014)


def test_video_estimate_default_seconds(settings: Settings) -> None:
    omni = replace(settings.catalog.models["gemini-omni-1.1-flash"], estimate_seconds=0)
    assert estimate_video_cost(omni, "720p", "", 0) == pytest.approx(1.014)


def test_ledger_totals(tmp_path: Path) -> None:
    ledger = CostLedger(tmp_path / "log.jsonl")
    now = datetime(2026, 10, 31, 12, 0, tzinfo=TZ)
    for when, cost, model in [
        (now, 0.10, "a"),
        (now - timedelta(days=1), 0.20, "a"),
        (now - timedelta(days=40), 0.40, "b"),
        (now, 0.0, "a"),
    ]:
        ledger.append(CostEntry(when.isoformat(), "generate_image", model, cost))
    with ledger.path.open("a") as fh:
        fh.write("{broken\n\n")
        fh.write(json.dumps({"timestamp": "nonsense", "estimated_cost_usd": 9}) + "\n")
    totals = ledger.totals(now)
    assert totals.today == pytest.approx(0.10)
    assert totals.month == pytest.approx(0.30)
    assert totals.total == pytest.approx(0.70)
    assert totals.calls == 3
    assert totals.by_model == {"a": pytest.approx(0.30), "b": pytest.approx(0.40)}


def test_ledger_empty(tmp_path: Path) -> None:
    assert CostLedger(tmp_path / "none.jsonl").totals(datetime.now(TZ)).total == 0


def test_ledger_entry_fields(tmp_path: Path) -> None:
    ledger = CostLedger(tmp_path / "log.jsonl")
    ledger.append(CostEntry("2026-10-31T12:00:00+01:00", "edit_image", "m", 0.05, "/x.png"))
    entry = ledger.entries()[0]
    assert set(entry) >= {"timestamp", "tool", "model", "estimated_cost_usd", "output_file"}


def test_budget_checks(settings: Settings) -> None:
    check_budget(0.10, settings, CostTotals())
    with pytest.raises(BudgetExceededError, match="per-call"):
        check_budget(0.51, settings, CostTotals())
    with pytest.raises(BudgetExceededError, match="Daily"):
        check_budget(0.30, settings, CostTotals(today=1.80, month=1.80))
    with pytest.raises(BudgetExceededError, match="Monthly"):
        check_budget(0.30, settings, CostTotals(today=0.0, month=19.80))
