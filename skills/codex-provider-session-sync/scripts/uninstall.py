#!/usr/bin/env python3
"""Remove the background service and optionally remove the installed skill."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Optional, Sequence


SKILL_NAME = "codex-provider-session-sync"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")),
    )
    parser.add_argument("--keep-skill", action="store_true")
    parser.add_argument("--remove-runtime-data", action="store_true")
    arguments = parser.parse_args(argv)

    codex_home = arguments.codex_home.expanduser().resolve()
    skill = codex_home / "skills" / SKILL_NAME
    scripts = skill / "scripts"
    if str(scripts) not in os.sys.path:
        os.sys.path.insert(0, str(scripts))
    from service_manager import uninstall_service

    result = uninstall_service(
        codex_home, remove_data=arguments.remove_runtime_data
    )
    if not arguments.keep_skill and skill.exists():
        shutil.rmtree(skill)
        result["skill_removed"] = str(skill)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
