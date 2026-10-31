"""Settings: model catalogue, prices, budgets and allowed directories.

Everything is read from environment variables (and an optional TOML file). The API key is
deliberately *not* part of these settings, so it can never end up in a repr or a log line.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Literal

ModelKind = Literal["image", "video"]

ENV_PREFIX = "GEMINI_MEDIA_"
DEFAULT_OUTPUT_DIR = Path.home() / "gemini-media"


class ConfigError(ValueError):
    """Invalid configuration (bad env value or config file)."""


@dataclass(frozen=True)
class ModelInfo:
    """One model with its price list.

    For image models ``output_prices`` maps image size to USD per image; for video
    models it maps resolution to USD per second.
    """

    id: str
    kind: ModelKind
    label: str
    input_usd_per_mtok: float
    output_prices: Mapping[str, float]
    aspect_ratios: tuple[str, ...]
    max_reference_images: int = 0
    estimate_seconds: int = 0
    notes: str = ""


@dataclass(frozen=True)
class Catalog:
    models: Mapping[str, ModelInfo]
    default_image_model: str
    default_video_model: str
    pricing_checked: str
    pricing_source: str


@dataclass(frozen=True)
class Settings:
    output_dir: Path
    allowed_dirs: tuple[Path, ...]
    state_dir: Path
    max_cost_per_call: float
    daily_budget: float
    monthly_budget: float
    max_input_bytes: int
    request_timeout_s: float
    catalog: Catalog

    @property
    def cost_log_path(self) -> Path:
        return self.state_dir / "cost-log.jsonl"

    @property
    def jobs_path(self) -> Path:
        return self.state_dir / "video-jobs.json"


def _parse_model(model_id: str, raw: Mapping[str, Any]) -> ModelInfo:
    try:
        kind = raw["kind"]
        if kind not in ("image", "video"):
            raise ConfigError(f"Model {model_id!r}: kind must be 'image' or 'video'")
        prices = {str(k): float(v) for k, v in raw["output_prices"].items()}
        if not prices or any(p < 0 for p in prices.values()):
            raise ConfigError(f"Model {model_id!r}: output_prices must be non-empty and >= 0")
        return ModelInfo(
            id=model_id,
            kind=kind,
            label=str(raw.get("label", model_id)),
            input_usd_per_mtok=float(raw.get("input_usd_per_mtok", 0.0)),
            output_prices=prices,
            aspect_ratios=tuple(str(a) for a in raw["aspect_ratios"]),
            max_reference_images=int(raw.get("max_reference_images", 0)),
            estimate_seconds=int(raw.get("estimate_seconds", 0)),
            notes=str(raw.get("notes", "")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(f"Model {model_id!r}: invalid entry ({exc})") from exc


def load_catalog(env: Mapping[str, str]) -> Catalog:
    """Load the bundled catalogue, merge an optional user TOML, apply env overrides."""
    data = tomllib.loads(
        resources.files("gemini_media_mcp").joinpath("models.toml").read_text("utf-8")
    )
    override_path = env.get(f"{ENV_PREFIX}CONFIG")
    if override_path:
        path = Path(override_path).expanduser()
        try:
            override = tomllib.loads(path.read_text("utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"Cannot read config file {path}: {exc}") from exc
        data.setdefault("models", {}).update(override.get("models", {}))
        data.setdefault("defaults", {}).update(override.get("defaults", {}))
        for key in ("pricing_checked", "pricing_source"):
            if key in override:
                data[key] = override[key]

    models = {mid: _parse_model(mid, raw) for mid, raw in data.get("models", {}).items()}
    defaults = data.get("defaults", {})
    image_default = env.get(f"{ENV_PREFIX}DEFAULT_IMAGE_MODEL") or defaults.get("image_model")
    video_default = env.get(f"{ENV_PREFIX}DEFAULT_VIDEO_MODEL") or defaults.get("video_model")
    for kind, model_id in (("image", image_default), ("video", video_default)):
        if model_id not in models or models[model_id].kind != kind:
            raise ConfigError(f"Default {kind} model {model_id!r} is not a configured {kind} model")
    return Catalog(
        models=models,
        default_image_model=str(image_default),
        default_video_model=str(video_default),
        pricing_checked=str(data.get("pricing_checked", "unknown")),
        pricing_source=str(data.get("pricing_source", "")),
    )


def _float_env(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(ENV_PREFIX + name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{ENV_PREFIX}{name} must be a number, got {raw!r}") from exc
    if value < 0:
        raise ConfigError(f"{ENV_PREFIX}{name} must be >= 0")
    return value


def _resolve_dir(raw: str) -> Path:
    return Path(raw).expanduser().resolve()


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build settings from the environment. Creates the output and state directories."""
    env = os.environ if env is None else env
    output_dir = _resolve_dir(env.get(f"{ENV_PREFIX}OUTPUT_DIR") or str(DEFAULT_OUTPUT_DIR))

    raw_allowed = env.get(f"{ENV_PREFIX}ALLOWED_DIRS", "")
    allowed = tuple(_resolve_dir(p) for p in raw_allowed.split(os.pathsep) if p.strip())
    if not allowed:
        allowed = (output_dir,)
    if not any(output_dir.is_relative_to(a) for a in allowed):
        raise ConfigError(f"Output directory {output_dir} must be inside {ENV_PREFIX}ALLOWED_DIRS")

    state_dir = _resolve_dir(env.get(f"{ENV_PREFIX}STATE_DIR") or str(output_dir / ".gemini-media"))
    output_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        output_dir=output_dir,
        allowed_dirs=allowed,
        state_dir=state_dir,
        max_cost_per_call=_float_env(env, "MAX_COST_PER_CALL", 0.50),
        daily_budget=_float_env(env, "DAILY_BUDGET", 2.00),
        monthly_budget=_float_env(env, "MONTHLY_BUDGET", 20.00),
        max_input_bytes=int(_float_env(env, "MAX_INPUT_MB", 7.0) * 1024 * 1024),
        request_timeout_s=_float_env(env, "TIMEOUT_SECONDS", 180.0),
        catalog=load_catalog(env),
    )
