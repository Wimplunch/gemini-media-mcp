"""Small persistent store for video jobs, so status survives a server restart."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class JobStore:
    """JSON file mapping job ID -> job metadata. Written atomically."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, jobs: dict[str, dict[str, Any]]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(jobs, indent=2, ensure_ascii=False) + "\n", "utf-8")
        tmp.replace(self.path)

    def get(self, job_id: str) -> dict[str, Any] | None:
        return self._load().get(job_id)

    def put(self, job_id: str, job: dict[str, Any]) -> None:
        jobs = self._load()
        jobs[job_id] = job
        self._save(jobs)

    def update(self, job_id: str, **fields: Any) -> dict[str, Any]:
        jobs = self._load()
        job = jobs[job_id]
        job.update(fields)
        self._save(jobs)
        return job
