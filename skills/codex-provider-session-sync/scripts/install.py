#!/usr/bin/env python3
"""Install the skill and its background synchronization service."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Optional, Sequence


SKILL_NAME = "codex-provider-session-sync"


def codex_home_from_env() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()


def source_skill_directory() -> Path:
    return Path(__file__).resolve().parents[1]


def copy_skill(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".installing")
    if temporary.exists():
        shutil.rmtree(temporary)
    shutil.copytree(source, temporary)
    if destination.exists():
        shutil.rmtree(destination)
    os.replace(str(temporary), str(destination))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path, default=codex_home_from_env())
    parser.add_argument(
        "--source-skill",
        type=Path,
        default=source_skill_directory(),
        help="Skill source directory used by the bootstrap installer",
    )
    arguments = parser.parse_args(argv)

    if sys.version_info < (3, 9):
        parser.error("Python 3.9 or newer is required")
    source = arguments.source_skill.expanduser().resolve()
    codex_home = arguments.codex_home.expanduser().resolve()
    destination = codex_home / "skills" / SKILL_NAME
    sys.path.insert(0, str(source / "scripts"))
    from service_manager import uninstall_service

    uninstall_service(codex_home)
    copy_skill(source, destination)

    sys.path.insert(0, str(destination / "scripts"))
    from provider_session_sync import sync_once
    from service_manager import install_service

    result = sync_once(codex_home)
    service = install_service(
        codex_home, destination / "scripts" / "provider_session_sync.py"
    )
    print(
        json.dumps(
            {
                "skill": str(destination),
                "sync": result.as_dict(),
                "service": service,
                "restart_codex": True,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
