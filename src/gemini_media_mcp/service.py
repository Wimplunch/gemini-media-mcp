"""Tool logic, independent of MCP: validation, cost control, output files and provenance."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from .backend import GeneratedMedia, MediaBackend
from .config import ModelInfo, ModelKind, Settings
from .costs import (
    CostEntry,
    CostLedger,
    check_budget,
    estimate_image_cost,
    estimate_video_cost,
)
from .files import InputImage, load_input_image, slugify, write_new_output, write_sidecar
from .jobs import JobStore
from .openrouter import VIDEO_UNAVAILABLE

MAX_PROMPT_CHARS = 10_000
AI_NOTICE = (
    "AI-generated with Google Gemini. Google embeds an invisible SynthID watermark. "
    "Label it as AI-generated when you publish it (EU AI Act Art. 50)."
)


class ToolInputError(ValueError):
    """Invalid tool arguments."""


def _round(value: float) -> float:
    return round(value, 4)


class MediaService:
    def __init__(
        self,
        settings: Settings,
        backend_factory: Callable[[], MediaBackend],
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self.settings = settings
        self._backend_factory = backend_factory
        self._backend: MediaBackend | None = None
        self._clock = clock
        self.ledger = CostLedger(settings.cost_log_path)
        self.jobs = JobStore(settings.jobs_path)
        # Serialises paid calls so two parallel requests cannot both pass the budget check.
        self._paid_call_lock = asyncio.Lock()

    @property
    def backend(self) -> MediaBackend:
        if self._backend is None:
            self._backend = self._backend_factory()
        return self._backend

    # --- validation helpers -------------------------------------------------------------

    def _model(self, model_id: str | None, kind: ModelKind) -> ModelInfo:
        catalog = self.settings.catalog
        if not model_id:
            model_id = (
                catalog.default_image_model if kind == "image" else catalog.default_video_model
            )
        model = catalog.models.get(model_id)
        if model is None or model.kind != kind:
            options = ", ".join(m.id for m in catalog.models.values() if m.kind == kind)
            raise ToolInputError(f"Unknown {kind} model {model_id!r}. Configured: {options}")
        return model

    @staticmethod
    def _prompt(prompt: str) -> str:
        prompt = prompt.strip()
        if not prompt:
            raise ToolInputError("The prompt must not be empty.")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ToolInputError(f"The prompt is longer than {MAX_PROMPT_CHARS} characters.")
        return prompt

    @staticmethod
    def _choice(value: str, allowed: Sequence[str], what: str, model: ModelInfo) -> str:
        if value not in allowed:
            raise ToolInputError(
                f"{what} {value!r} is not available for {model.id}. Options: {', '.join(allowed)}"
            )
        return value

    def _images(self, paths: Sequence[str]) -> list[InputImage]:
        return [
            load_input_image(p, self.settings.allowed_dirs, self.settings.max_input_bytes)
            for p in paths
        ]

    def _save(
        self,
        media: GeneratedMedia,
        name: str | None,
        prompt: str,
        now: datetime,
        metadata: dict[str, Any],
    ) -> tuple[Path, Path]:
        slug = slugify(name or prompt, fallback="media")
        path = write_new_output(self.settings.output_dir, slug, media.mime_type, media.data, now)
        sidecar = write_sidecar(
            path,
            {
                "file": path.name,
                "created": now.isoformat(timespec="seconds"),
                "generator": f"gemini-media-mcp {__version__}",
                "provider": self.settings.provider,
                "prompt": prompt,
                **metadata,
                "model_text": media.text,
                "ai_generated": True,
                "notice": AI_NOTICE,
            },
        )
        return path, sidecar

    @staticmethod
    def _image_refs(images: Sequence[InputImage]) -> list[dict[str, str]]:
        return [{"path": str(i.path), "sha256": i.sha256, "mime_type": i.mime_type} for i in images]

    # --- tools ------------------------------------------------------------------------

    async def generate_image(
        self,
        prompt: str,
        model: str | None = None,
        aspect_ratio: str = "1:1",
        resolution: str = "1K",
        output_name: str | None = None,
    ) -> dict[str, Any]:
        return await self._image_call(
            "generate_image", prompt, [], model, aspect_ratio, resolution, output_name
        )

    async def edit_image(
        self,
        prompt: str,
        image_paths: Sequence[str],
        model: str | None = None,
        aspect_ratio: str | None = None,
        resolution: str = "1K",
        output_name: str | None = None,
    ) -> dict[str, Any]:
        if not image_paths:
            raise ToolInputError("Give at least one input image path.")
        return await self._image_call(
            "edit_image", prompt, image_paths, model, aspect_ratio, resolution, output_name
        )

    async def _image_call(
        self,
        tool: str,
        prompt: str,
        image_paths: Sequence[str],
        model_id: str | None,
        aspect_ratio: str | None,
        resolution: str,
        output_name: str | None,
    ) -> dict[str, Any]:
        prompt = self._prompt(prompt)
        model = self._model(model_id, "image")
        if aspect_ratio is not None:
            self._choice(aspect_ratio, model.aspect_ratios, "Aspect ratio", model)
        self._choice(resolution, list(model.output_prices), "Resolution", model)
        if len(image_paths) > model.max_reference_images:
            raise ToolInputError(
                f"{model.id} accepts at most {model.max_reference_images} reference images."
            )
        images = self._images(image_paths)
        estimate = estimate_image_cost(model, resolution, prompt, len(images))

        async with self._paid_call_lock:
            check_budget(estimate, self.settings, self.ledger.totals(self._clock()))
            media = await self.backend.generate_image(
                model=model.id,
                prompt=prompt,
                images=images,
                aspect_ratio=aspect_ratio,
                image_size=resolution,
            )
            now = self._clock()
            params = {"aspect_ratio": aspect_ratio, "resolution": resolution}
            path, sidecar = self._save(
                media,
                output_name,
                prompt,
                now,
                {
                    "tool": tool,
                    "model": model.id,
                    "reference_images": self._image_refs(images),
                    "parameters": params,
                    "estimated_cost_usd": _round(estimate),
                },
            )
            self.ledger.append(
                CostEntry(
                    timestamp=now.isoformat(timespec="seconds"),
                    tool=tool,
                    model=model.id,
                    estimated_cost_usd=_round(estimate),
                    output_file=str(path),
                )
            )
        return {
            "file": str(path),
            "sidecar": str(sidecar),
            "model": model.id,
            "estimated_cost_usd": _round(estimate),
            "model_text": media.text,
            "notice": AI_NOTICE,
        }

    async def start_video(
        self,
        prompt: str,
        model: str | None = None,
        start_image: str | None = None,
        aspect_ratio: str = "16:9",
        resolution: str = "720p",
        output_name: str | None = None,
        confirm_cost: bool = False,
    ) -> dict[str, Any]:
        if self.settings.provider != "google":
            raise ToolInputError(VIDEO_UNAVAILABLE)
        prompt = self._prompt(prompt)
        info = self._model(model, "video")
        self._choice(aspect_ratio, info.aspect_ratios, "Aspect ratio", info)
        self._choice(resolution, list(info.output_prices), "Resolution", info)
        images = self._images([start_image]) if start_image else []
        estimate = estimate_video_cost(info, resolution, prompt, len(images))

        if not confirm_cost:
            return {
                "status": "estimate_only",
                "model": info.id,
                "estimated_cost_usd": _round(estimate),
                "assumed_seconds": info.estimate_seconds,
                "message": (
                    f"Nothing was started. This video will cost about ${estimate:.2f}. "
                    "Call start_video again with confirm_cost=true to start it."
                ),
            }

        async with self._paid_call_lock:
            check_budget(estimate, self.settings, self.ledger.totals(self._clock()))
            job_id = await self.backend.start_video(
                model=info.id,
                prompt=prompt,
                image=images[0] if images else None,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
            )
            now = self._clock()
            # Logged at start: Google bills the generation even if we never download it.
            self.ledger.append(
                CostEntry(
                    timestamp=now.isoformat(timespec="seconds"),
                    tool="start_video",
                    model=info.id,
                    estimated_cost_usd=_round(estimate),
                    job_id=job_id,
                )
            )
            self.jobs.put(
                job_id,
                {
                    "status": "in_progress",
                    "created": now.isoformat(timespec="seconds"),
                    "model": info.id,
                    "prompt": prompt,
                    "output_name": output_name,
                    "reference_images": self._image_refs(images),
                    "parameters": {"aspect_ratio": aspect_ratio, "resolution": resolution},
                    "estimated_cost_usd": _round(estimate),
                },
            )
        return {
            "status": "started",
            "job_id": job_id,
            "model": info.id,
            "estimated_cost_usd": _round(estimate),
            "message": "Video generation takes minutes. Poll get_video_status with the job_id.",
        }

    async def get_video_status(self, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job is None:
            raise ToolInputError(f"Unknown job_id {job_id!r} (only jobs started here are known).")
        if job["status"] in ("completed", "failed"):
            return {"job_id": job_id, **{k: job.get(k) for k in ("status", "file", "error")}}

        state = await self.backend.get_video(job_id)
        if state.status == "in_progress":
            started = datetime.fromisoformat(job["created"])
            elapsed = int((self._clock() - started).total_seconds())
            return {"job_id": job_id, "status": "in_progress", "elapsed_seconds": elapsed}
        if state.status != "completed" or state.video is None:
            self.jobs.update(job_id, status="failed", error=state.error)
            return {"job_id": job_id, "status": "failed", "error": state.error}

        now = self._clock()
        path, sidecar = self._save(
            state.video,
            job.get("output_name"),
            job["prompt"],
            now,
            {
                "tool": "start_video",
                "model": job["model"],
                "job_id": job_id,
                "reference_images": job.get("reference_images", []),
                "parameters": job.get("parameters", {}),
                "estimated_cost_usd": job.get("estimated_cost_usd"),
            },
        )
        self.ledger.append(
            CostEntry(
                timestamp=now.isoformat(timespec="seconds"),
                tool="get_video_status",
                model=job["model"],
                estimated_cost_usd=0.0,
                output_file=str(path),
                job_id=job_id,
                note="download; cost was logged at start_video",
            )
        )
        self.jobs.update(job_id, status="completed", file=str(path))
        return {
            "job_id": job_id,
            "status": "completed",
            "file": str(path),
            "sidecar": str(sidecar),
            "notice": AI_NOTICE,
        }

    def list_models(self) -> dict[str, Any]:
        catalog = self.settings.catalog
        models = [
            {
                "id": m.id,
                "name": m.label,
                "kind": m.kind,
                "default": m.id in (catalog.default_image_model, catalog.default_video_model),
                "price_unit": "USD per image" if m.kind == "image" else "USD per second",
                "prices": dict(m.output_prices),
                "input_usd_per_million_tokens": m.input_usd_per_mtok,
                "aspect_ratios": list(m.aspect_ratios),
                "max_reference_images": m.max_reference_images,
                "notes": m.notes,
            }
            for m in catalog.models.values()
        ]
        return {
            "provider": self.settings.provider,
            "video_available": self.settings.provider == "google",
            "models": models,
            "pricing_checked": catalog.pricing_checked,
            "pricing_source": catalog.pricing_source,
            "disclaimer": "Prices are estimates from Google's public price list; your bill rules.",
        }

    def cost_report(self) -> dict[str, Any]:
        totals = self.ledger.totals(self._clock())
        s = self.settings
        return {
            "today_usd": _round(totals.today),
            "month_usd": _round(totals.month),
            "total_usd": _round(totals.total),
            "paid_calls": totals.calls,
            "by_model_usd": {k: _round(v) for k, v in sorted(totals.by_model.items())},
            "limits": {
                "max_cost_per_call_usd": s.max_cost_per_call,
                "daily_budget_usd": s.daily_budget,
                "monthly_budget_usd": s.monthly_budget,
                "daily_remaining_usd": _round(max(0.0, s.daily_budget - totals.today)),
                "monthly_remaining_usd": _round(max(0.0, s.monthly_budget - totals.month)),
            },
            "cost_log": str(s.cost_log_path),
            "note": "Estimated costs from the local log, not your provider's billing data.",
        }
