#!/usr/bin/env python3
"""Bootstrap Codex Provider Session Sync from a checkout or GitHub."""

from __future__ import annotations

import argparse
import os
import runpy
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional, Sequence


REPOSITORY = "Troyefall/codex-provider-session-sync"
ARCHIVE_URL = "https://github.com/{}/archive/refs/heads/main.zip".format(REPOSITORY)
SKILL_RELATIVE = Path("skills") / "codex-provider-session-sync"


def local_skill() -> Optional[Path]:
    try:
        root = Path(__file__).resolve().parent
    except NameError:
        return None
    candidate = root / SKILL_RELATIVE
    return candidate if (candidate / "SKILL.md").is_file() else None


def download_skill(working_directory: Path) -> Path:
    archive = working_directory / "source.zip"
    request = urllib.request.Request(
        ARCHIVE_URL,
        headers={"User-Agent": "codex-provider-session-sync-installer"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        archive.write_bytes(response.read())
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(working_directory)
    roots = [
        path
        for path in working_directory.iterdir()
        if path.is_dir() and path.name.startswith("codex-provider-session-sync-")
    ]
    if len(roots) != 1:
        raise RuntimeError("The downloaded repository archive is invalid")
    skill = roots[0] / SKILL_RELATIVE
    if not (skill / "SKILL.md").is_file():
        raise RuntimeError("The downloaded repository contains no installable skill")
    return skill


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--codex-home",
        default=os.environ.get("CODEX_HOME", str(Path.home() / ".codex")),
    )
    arguments = parser.parse_args(argv)
    if sys.version_info < (3, 9):
        parser.error("Python 3.9 or newer is required")

    source = local_skill()
    temporary_root = None
    if source is None:
        temporary_root = Path(tempfile.mkdtemp(prefix="codex-provider-sync-"))
        source = download_skill(temporary_root)
    try:
        installer = source / "scripts" / "install.py"
        sys.argv = [
            str(installer),
            "--codex-home",
            arguments.codex_home,
            "--source-skill",
            str(source),
        ]
        try:
            runpy.run_path(str(installer), run_name="__main__")
        except SystemExit as exc:
            return int(exc.code or 0)
        return 0
    finally:
        if temporary_root is not None:
            shutil.rmtree(temporary_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
