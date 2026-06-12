#!/usr/bin/env python3
"""Run a dependency-free validation of the installed Skill structure."""

from __future__ import annotations

import re
import sys
from pathlib import Path


NAME_PATTERN = re.compile(r"^[a-z0-9-]{1,64}$")


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: validate_skill.py PATH", file=sys.stderr)
        return 2
    skill = Path(sys.argv[1])
    skill_file = skill / "SKILL.md"
    agent_file = skill / "agents" / "openai.yaml"
    required_scripts = (
        "provider_session_sync.py",
        "service_manager.py",
        "install.py",
        "uninstall.py",
    )
    errors = []
    if not skill_file.is_file():
        errors.append("SKILL.md is missing")
    else:
        text = skill_file.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            errors.append("SKILL.md must start with YAML frontmatter")
        name_match = re.search(r"^name:\s*(.+)$", text, re.MULTILINE)
        description_match = re.search(
            r"^description:\s*(.+)$", text, re.MULTILINE
        )
        if not name_match or not NAME_PATTERN.match(name_match.group(1).strip()):
            errors.append("Skill name is missing or invalid")
        if not description_match or len(description_match.group(1).strip()) < 40:
            errors.append("Skill description is missing or too short")
    if not agent_file.is_file():
        errors.append("agents/openai.yaml is missing")
    for script in required_scripts:
        if not (skill / "scripts" / script).is_file():
            errors.append("scripts/{} is missing".format(script))
    if errors:
        for error in errors:
            print("ERROR: {}".format(error), file=sys.stderr)
        return 1
    print("Skill validation passed: {}".format(skill))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
