from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


class PathResolver:
    """Centralized path resolver for portable deployments.

    Priority:
    1. explicit data_dir argument
    2. env `YUKIKO_DATA_DIR`
    3. `<project_root>/storage`
    """

    def __init__(self, project_root: Path, data_dir: str | Path | None = None):
        self.project_root = Path(project_root).expanduser().resolve()

        raw_data_dir = str(data_dir or "").strip()
        if not raw_data_dir:
            raw_data_dir = str(os.getenv("YUKIKO_DATA_DIR", "")).strip()
        if raw_data_dir:
            p = Path(raw_data_dir).expanduser()
            if not p.is_absolute():
                p = self.project_root / p
            self.data_dir = p.resolve()
        else:
            self.data_dir = (self.project_root / "storage").resolve()

        self.data_dir.mkdir(parents=True, exist_ok=True)

    def data(self, *parts: str | Path) -> Path:
        target = self.data_dir
        for part in parts:
            target = target / Path(str(part))
        return target.resolve()

    def project(self, *parts: str | Path) -> Path:
        target = self.project_root
        for part in parts:
            target = target / Path(str(part))
        return target.resolve()

    @staticmethod
    def ensure_relative(path: str | Path, base_dir: Path) -> str:
        p = Path(path).expanduser()
        try:
            p = p.resolve()
        except Exception:
            p = p
        b = Path(base_dir).expanduser()
        try:
            b = b.resolve()
        except Exception:
            b = b
        try:
            return p.relative_to(b).as_posix()
        except Exception:
            return p.as_posix()

    @staticmethod
    def resolve_relative(
        raw: str | Path,
        base_dir: Path,
        fallback_roots: Iterable[Path] | None = None,
    ) -> Path:
        raw_text = str(raw or "").strip()
        if not raw_text:
            return Path(base_dir).resolve()

        candidate = Path(raw_text).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()

        base = Path(base_dir).expanduser()
        first = (base / candidate).resolve()
        if first.exists():
            return first

        for root in fallback_roots or []:
            try:
                test = (Path(root).expanduser() / candidate).resolve()
            except Exception:
                continue
            if test.exists():
                return test
        return first
