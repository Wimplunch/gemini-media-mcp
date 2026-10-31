"""Cost estimation, the JSONL cost log and budget enforcement."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import ModelInfo, Settings

logger = logging.getLogger(__name__)

# Rough token counts for the (small) input side of the estimate.
TOKENS_PER_INPUT_IMAGE = 1120
CHARS_PER_TOKEN = 3


class BudgetExceededError(Exception):
    """The estimated cost would break a configured limit."""


def _input_cost(model: ModelInfo, prompt: str, n_images: int) -> float:
    tokens = len(prompt) / CHARS_PER_TOKEN + n_images * TOKENS_PER_INPUT_IMAGE
    return tokens * model.input_usd_per_mtok / 1_000_000


def estimate_image_cost(model: ModelInfo, image_size: str, prompt: str, n_images: int) -> float:
    """USD estimate for one generated image (output price + input tokens)."""
    return model.output_prices[image_size] + _input_cost(model, prompt, n_images)


def estimate_video_cost(model: ModelInfo, resolution: str, prompt: str, n_images: int) -> float:
    """USD estimate for one video, assuming the model's configured (maximum) length."""
    seconds = model.estimate_seconds or 10
    return model.output_prices[resolution] * seconds + _input_cost(model, prompt, n_images)


@dataclass(frozen=True)
class CostEntry:
    timestamp: str
    tool: str
    model: str
    estimated_cost_usd: float
    output_file: str | None = None
    job_id: str | None = None
    note: str | None = None


@dataclass
class CostTotals:
    today: float = 0.0
    month: float = 0.0
    total: float = 0.0
    calls: int = 0
    by_model: dict[str, float] = field(default_factory=lambda: defaultdict(float))


class CostLedger:
    """Append-only JSONL log of estimated costs."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, entry: CostEntry) -> None:
        line = json.dumps(asdict(entry), ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        result: list[dict[str, Any]] = []
        with self.path.open(encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    result.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("Skipping corrupt cost log line %d", lineno)
        return result

    def totals(self, now: datetime) -> CostTotals:
        """Sum costs for today, the current month and overall (in now's time zone)."""
        totals = CostTotals()
        for entry in self.entries():
            try:
                cost = float(entry.get("estimated_cost_usd", 0.0))
                when = datetime.fromisoformat(entry["timestamp"]).astimezone(now.tzinfo)
            except (KeyError, TypeError, ValueError):
                continue
            totals.total += cost
            if cost > 0:
                totals.calls += 1
            totals.by_model[str(entry.get("model", "?"))] += cost
            if (when.year, when.month) == (now.year, now.month):
                totals.month += cost
                if when.date() == now.date():
                    totals.today += cost
        return totals


def check_budget(estimate: float, settings: Settings, totals: CostTotals) -> None:
    """Raise BudgetExceededError if the estimate would break any configured limit."""
    if estimate > settings.max_cost_per_call:
        raise BudgetExceededError(
            f"Estimated cost ${estimate:.4f} exceeds the per-call limit "
            f"${settings.max_cost_per_call:.2f} (GEMINI_MEDIA_MAX_COST_PER_CALL)."
        )
    if totals.today + estimate > settings.daily_budget:
        raise BudgetExceededError(
            f"Daily budget would be exceeded: ${totals.today:.4f} spent today + "
            f"${estimate:.4f} > ${settings.daily_budget:.2f} (GEMINI_MEDIA_DAILY_BUDGET)."
        )
    if totals.month + estimate > settings.monthly_budget:
        raise BudgetExceededError(
            f"Monthly budget would be exceeded: ${totals.month:.4f} spent this month + "
            f"${estimate:.4f} > ${settings.monthly_budget:.2f} (GEMINI_MEDIA_MONTHLY_BUDGET)."
        )
