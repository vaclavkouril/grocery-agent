"""Publish isolated report artifacts; local CLI latest aliases are opt-in."""

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from grocery_agent.application.services import MealExecution
from grocery_agent.meals.report import atomic_write, render_html, save_report


@dataclass(frozen=True)
class ReportArtifacts:
    request_id: UUID
    directory: Path
    html_path: Path
    json_path: Path
    request_path: Path


class FileMealReportStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def save(self, execution: MealExecution, *, publish_latest: bool = False) -> ReportArtifacts:
        parent = self.root / "requests"
        parent.mkdir(parents=True, exist_ok=True)
        directory = parent / str(execution.request_id)
        if directory.exists():
            raise FileExistsError(f"request report already exists: {execution.request_id}")
        staging = Path(tempfile.mkdtemp(prefix=".report-", dir=parent))
        try:
            atomic_write(
                staging / "report.json",
                execution.report.model_dump_json(indent=2, exclude_computed_fields=True),
            )
            atomic_write(
                staging / "report.html", render_html(execution.report, execution.catalog_snapshot)
            )
            metadata = execution.model_dump(mode="json", exclude={"report"})
            metadata["run_id"] = execution.report.run_id
            atomic_write(staging / "request.json", json.dumps(metadata, indent=2))
            # A completed target is nonempty, so a racing rename cannot replace it on Linux.
            os.rename(staging, directory)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        if publish_latest:
            save_report(self.root, execution.report, execution.catalog_snapshot)
        return ReportArtifacts(
            execution.request_id,
            directory,
            directory / "report.html",
            directory / "report.json",
            directory / "request.json",
        )
