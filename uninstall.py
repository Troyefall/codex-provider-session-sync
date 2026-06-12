#!/usr/bin/env python3
"""Uninstall Codex Provider Session Sync while retaining backups."""

from __future__ import annotations

import argparse
import os
import runpy
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--codex-home",
        default=os.environ.get("CODEX_HOME", str(Path.home() / ".codex")),
    )
    parser.add_argument("--keep-skill", action="store_true")
    parser.add_argument("--remove-runtime-data", action="store_true")
    arguments = parser.parse_args()
    script = (
        Path(arguments.codex_home).expanduser()
        / "skills"
        / "codex-provider-session-sync"
        / "scripts"
        / "uninstall.py"
    )
    if not script.is_file():
        parser.error("The installed uninstaller was not found: {}".format(script))
    sys.argv = [str(script), "--codex-home", arguments.codex_home]
    if arguments.keep_skill:
        sys.argv.append("--keep-skill")
    if arguments.remove_runtime_data:
        sys.argv.append("--remove-runtime-data")
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
