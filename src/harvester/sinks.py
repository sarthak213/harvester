"""Output: one JSONL file of records per run, plus a manifest describing the run."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import orjson

from harvester.models import Provenance, Record


class JsonlSink:
    """Appends ``{"kind", "id", "data", "provenance"}`` lines to ``records.jsonl``."""

    def __init__(self, run_dir: Path) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        self.path = run_dir / "records.jsonl"
        self._file = self.path.open("wb")
        self.counts: Counter[str] = Counter()

    def write(self, record: Record, provenance: Provenance) -> None:
        line = {
            "kind": record.kind,
            "id": record.id,
            "data": record.data,
            "provenance": provenance.model_dump(mode="json", exclude_none=True),
        }
        self._file.write(orjson.dumps(line, option=orjson.OPT_NON_STR_KEYS) + b"\n")
        self.counts[record.kind] += 1

    def close(self) -> None:
        self._file.close()


def write_manifest(run_dir: Path, manifest: dict[str, Any]) -> Path:
    path = run_dir / "manifest.json"
    path.write_bytes(orjson.dumps(manifest, option=orjson.OPT_INDENT_2 | orjson.OPT_NON_STR_KEYS))
    return path
