from __future__ import annotations

from pathlib import Path

import pytest

from gemini_media_mcp.config import ConfigError, load_settings


def test_defaults(out_dir: Path) -> None:
    s = load_settings({"GEMINI_MEDIA_OUTPUT_DIR": str(out_dir)})
    assert s.output_dir == out_dir.resolve()
    assert s.allowed_dirs == (out_dir.resolve(),)
    assert s.max_cost_per_call == 0.50
    assert s.daily_budget == 2.0
    assert s.monthly_budget == 20.0
    assert s.max_input_bytes == 7 * 1024 * 1024
    assert s.state_dir.is_dir()
    assert s.catalog.default_image_model == "gemini-nano-banana-2.1"
    assert s.catalog.default_video_model == "gemini-omni-1.1-flash"
    assert s.catalog.pricing_checked == "2026-10-31"


def test_env_overrides(tmp_path: Path, out_dir: Path) -> None:
    other = tmp_path / "refs"
    other.mkdir()
    s = load_settings(
        {
            "GEMINI_MEDIA_OUTPUT_DIR": str(out_dir),
            "GEMINI_MEDIA_ALLOWED_DIRS": f"{out_dir}:{other}",
            "GEMINI_MEDIA_MAX_COST_PER_CALL": "0.1",
            "GEMINI_MEDIA_DAILY_BUDGET": "1",
            "GEMINI_MEDIA_MONTHLY_BUDGET": "5",
            "GEMINI_MEDIA_MAX_INPUT_MB": "2",
            "GEMINI_MEDIA_DEFAULT_IMAGE_MODEL": "gemini-3.1-flash-lite-image",
        }
    )
    assert s.allowed_dirs == (out_dir.resolve(), other.resolve())
    assert (s.max_cost_per_call, s.daily_budget, s.monthly_budget) == (0.1, 1.0, 5.0)
    assert s.max_input_bytes == 2 * 1024 * 1024
    assert s.catalog.default_image_model == "gemini-3.1-flash-lite-image"


@pytest.mark.parametrize("value", ["abc", "-1"])
def test_invalid_numbers(out_dir: Path, value: str) -> None:
    with pytest.raises(ConfigError, match="DAILY_BUDGET"):
        load_settings({"GEMINI_MEDIA_OUTPUT_DIR": str(out_dir), "GEMINI_MEDIA_DAILY_BUDGET": value})


def test_output_must_be_inside_allowed(tmp_path: Path, out_dir: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with pytest.raises(ConfigError, match="inside"):
        load_settings(
            {"GEMINI_MEDIA_OUTPUT_DIR": str(out_dir), "GEMINI_MEDIA_ALLOWED_DIRS": str(elsewhere)}
        )


def test_default_model_must_exist(out_dir: Path) -> None:
    with pytest.raises(ConfigError, match="Default image model"):
        load_settings(
            {"GEMINI_MEDIA_OUTPUT_DIR": str(out_dir), "GEMINI_MEDIA_DEFAULT_IMAGE_MODEL": "nope"}
        )
    with pytest.raises(ConfigError, match="Default video model"):
        load_settings(
            {
                "GEMINI_MEDIA_OUTPUT_DIR": str(out_dir),
                "GEMINI_MEDIA_DEFAULT_VIDEO_MODEL": "gemini-3-pro-image",
            }
        )


def test_config_file_overrides(tmp_path: Path, out_dir: Path) -> None:
    cfg = tmp_path / "prices.toml"
    cfg.write_text(
        'pricing_checked = "2027-01-01"\n'
        '[models."gemini-omni-1.1-flash"]\n'
        'kind = "video"\nlabel = "Omni"\ninput_usd_per_mtok = 1.5\n'
        'output_prices = { "720p" = 0.2, "1080p" = 0.3 }\n'
        'aspect_ratios = ["16:9"]\nestimate_seconds = 8\n'
        '[models."my-model"]\nkind = "image"\noutput_prices = { "1K" = 0.01 }\n'
        'aspect_ratios = ["1:1"]\n'
    )
    s = load_settings({"GEMINI_MEDIA_OUTPUT_DIR": str(out_dir), "GEMINI_MEDIA_CONFIG": str(cfg)})
    omni = s.catalog.models["gemini-omni-1.1-flash"]
    assert omni.output_prices == {"720p": 0.2, "1080p": 0.3}
    assert omni.estimate_seconds == 8
    assert s.catalog.models["my-model"].label == "my-model"
    assert s.catalog.pricing_checked == "2027-01-01"


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ('[models.x]\nkind = "audio"\noutput_prices = {a = 1}\naspect_ratios = []\n', "kind"),
        ('[models.x]\nkind = "image"\noutput_prices = {}\naspect_ratios = []\n', "non-empty"),
        ('[models.x]\nkind = "image"\n', "invalid entry"),
        ("not = [valid toml", "Cannot read"),
    ],
)
def test_bad_config_file(tmp_path: Path, out_dir: Path, body: str, match: str) -> None:
    cfg = tmp_path / "bad.toml"
    cfg.write_text(body)
    with pytest.raises(ConfigError, match=match):
        load_settings({"GEMINI_MEDIA_OUTPUT_DIR": str(out_dir), "GEMINI_MEDIA_CONFIG": str(cfg)})


def test_missing_config_file(tmp_path: Path, out_dir: Path) -> None:
    with pytest.raises(ConfigError, match="Cannot read"):
        load_settings(
            {
                "GEMINI_MEDIA_OUTPUT_DIR": str(out_dir),
                "GEMINI_MEDIA_CONFIG": str(tmp_path / "missing.toml"),
            }
        )
