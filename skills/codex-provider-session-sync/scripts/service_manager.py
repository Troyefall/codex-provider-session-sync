#!/usr/bin/env python3
"""Install and remove the background watcher on supported platforms."""

from __future__ import annotations

import getpass
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional
from xml.sax.saxutils import escape


APP_NAME = "codex-provider-session-sync"
MACOS_LABEL = "io.github.troyefall.codex-provider-session-sync"
LEGACY_MACOS_LABEL = "com.openai.codex-provider-session-sync"
LINUX_UNIT = "codex-provider-session-sync.service"
WINDOWS_TASK = "Codex Provider Session Sync"


class ServiceError(RuntimeError):
    """Raised when a platform service cannot be managed."""


def _run(command: List[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _python_executable() -> Path:
    executable = Path(sys.executable).resolve()
    if os.name == "nt":
        pythonw = executable.with_name("pythonw.exe")
        if pythonw.is_file():
            return pythonw
    return executable


def _service_command(codex_home: Path, script_path: Path) -> List[str]:
    return [
        str(_python_executable()),
        str(script_path.resolve()),
        "--codex-home",
        str(codex_home),
        "watch",
    ]


def _write_json(path: Path, value: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _install_macos(codex_home: Path, script_path: Path) -> Dict[str, object]:
    uid = os.getuid()
    domain = "gui/{}".format(uid)
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    plist_path = launch_agents / "{}.plist".format(MACOS_LABEL)
    log_dir = codex_home / APP_NAME / "logs"
    launch_agents.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    plist = {
        "Label": MACOS_LABEL,
        "ProgramArguments": _service_command(codex_home, script_path),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 5,
        "StandardOutPath": str(log_dir / "service.stdout.log"),
        "StandardErrorPath": str(log_dir / "service.stderr.log"),
    }
    with plist_path.open("wb") as handle:
        plistlib.dump(plist, handle, sort_keys=True)

    legacy_path = launch_agents / "{}.plist".format(LEGACY_MACOS_LABEL)
    legacy_service_present = legacy_path.exists()
    _run(
        ["launchctl", "bootout", "{}/{}".format(domain, LEGACY_MACOS_LABEL)],
        check=False,
    )
    _run(
        ["launchctl", "bootout", "{}/{}".format(domain, MACOS_LABEL)],
        check=False,
    )
    legacy_path.unlink(missing_ok=True)
    _run(["launchctl", "bootstrap", domain, str(plist_path)])
    _run(
        ["launchctl", "enable", "{}/{}".format(domain, MACOS_LABEL)],
        check=False,
    )

    return {
        "platform": "macos",
        "service": MACOS_LABEL,
        "service_file": str(plist_path),
        "legacy_service_stopped": legacy_service_present,
    }


def _install_linux(codex_home: Path, script_path: Path) -> Dict[str, object]:
    if shutil.which("systemctl") is None:
        raise ServiceError("systemctl is required for Linux installation")
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_path = unit_dir / LINUX_UNIT
    unit_dir.mkdir(parents=True, exist_ok=True)
    command = _service_command(codex_home, script_path)
    quoted = " ".join(_systemd_quote(item) for item in command)
    unit_path.write_text(
        "\n".join(
            [
                "[Unit]",
                "Description=Codex Provider Session Sync",
                "After=default.target",
                "",
                "[Service]",
                "Type=simple",
                "ExecStart={}".format(quoted),
                "Restart=on-failure",
                "RestartSec=5",
                "",
                "[Install]",
                "WantedBy=default.target",
                "",
            ]
        ),
        encoding="utf-8",
    )
    _run(["systemctl", "--user", "daemon-reload"])
    _run(["systemctl", "--user", "enable", "--now", LINUX_UNIT])
    return {
        "platform": "linux",
        "service": LINUX_UNIT,
        "service_file": str(unit_path),
    }


def _systemd_quote(value: str) -> str:
    return '"{}"'.format(value.replace("\\", "\\\\").replace('"', '\\"'))


def _windows_task_xml(codex_home: Path, script_path: Path) -> str:
    command = _service_command(codex_home, script_path)
    executable = escape(command[0])
    arguments = escape(
        subprocess.list2cmdline(command[1:])
    )
    user = escape("{}\\{}".format(os.environ.get("USERDOMAIN", "."), getpass.getuser()))
    return """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Keep Codex sessions visible across provider switches.</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RestartOnFailure><Interval>PT1M</Interval><Count>10</Count></RestartOnFailure>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>{executable}</Command><Arguments>{arguments}</Arguments></Exec></Actions>
</Task>
""".format(user=user, executable=executable, arguments=arguments)


def _install_windows(codex_home: Path, script_path: Path) -> Dict[str, object]:
    xml = _windows_task_xml(codex_home, script_path)
    descriptor, name = tempfile.mkstemp(suffix=".xml")
    os.close(descriptor)
    task_file = Path(name)
    try:
        task_file.write_text(xml, encoding="utf-16")
        _run(
            [
                "schtasks.exe",
                "/Create",
                "/TN",
                WINDOWS_TASK,
                "/XML",
                str(task_file),
                "/F",
            ]
        )
        _run(["schtasks.exe", "/Run", "/TN", WINDOWS_TASK], check=False)
    finally:
        task_file.unlink(missing_ok=True)
    return {"platform": "windows", "service": WINDOWS_TASK}


def install_service(codex_home: Path, script_path: Path) -> Dict[str, object]:
    if sys.platform == "darwin":
        result = _install_macos(codex_home, script_path)
    elif sys.platform.startswith("linux"):
        result = _install_linux(codex_home, script_path)
    elif os.name == "nt":
        result = _install_windows(codex_home, script_path)
    else:
        raise ServiceError("Unsupported operating system: {}".format(sys.platform))
    _write_json(codex_home / APP_NAME / "service.json", result)
    return result


def service_status(codex_home: Path) -> Dict[str, object]:
    metadata_path = codex_home / APP_NAME / "service.json"
    metadata: Dict[str, object] = {}
    if metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            metadata = {}
    if sys.platform == "darwin":
        result = _run(
            [
                "launchctl",
                "print",
                "gui/{}/{}".format(os.getuid(), MACOS_LABEL),
            ],
            check=False,
        )
        installed = (
            Path.home()
            / "Library"
            / "LaunchAgents"
            / "{}.plist".format(MACOS_LABEL)
        ).is_file()
        running = result.returncode == 0
    elif sys.platform.startswith("linux"):
        installed = (
            Path.home() / ".config" / "systemd" / "user" / LINUX_UNIT
        ).is_file()
        result = _run(
            ["systemctl", "--user", "is-active", LINUX_UNIT], check=False
        )
        running = result.returncode == 0 and result.stdout.strip() == "active"
    elif os.name == "nt":
        result = _run(
            ["schtasks.exe", "/Query", "/TN", WINDOWS_TASK], check=False
        )
        installed = result.returncode == 0
        running = installed
    else:
        installed = False
        running = False
    return {
        "installed": installed,
        "running": running,
        "platform": metadata.get("platform"),
        "service": metadata.get("service"),
    }


def _uninstall_macos() -> Dict[str, object]:
    uid = os.getuid()
    domain = "gui/{}".format(uid)
    path = Path.home() / "Library" / "LaunchAgents" / "{}.plist".format(MACOS_LABEL)
    _run(
        ["launchctl", "bootout", "{}/{}".format(domain, MACOS_LABEL)],
        check=False,
    )
    path.unlink(missing_ok=True)
    return {"platform": "macos", "removed": MACOS_LABEL}


def _uninstall_linux() -> Dict[str, object]:
    if shutil.which("systemctl"):
        _run(["systemctl", "--user", "disable", "--now", LINUX_UNIT], check=False)
        _run(["systemctl", "--user", "daemon-reload"], check=False)
    path = Path.home() / ".config" / "systemd" / "user" / LINUX_UNIT
    path.unlink(missing_ok=True)
    return {"platform": "linux", "removed": LINUX_UNIT}


def _uninstall_windows() -> Dict[str, object]:
    _run(
        ["schtasks.exe", "/Delete", "/TN", WINDOWS_TASK, "/F"],
        check=False,
    )
    return {"platform": "windows", "removed": WINDOWS_TASK}


def uninstall_service(
    codex_home: Path, remove_data: bool = False
) -> Dict[str, object]:
    if sys.platform == "darwin":
        result = _uninstall_macos()
    elif sys.platform.startswith("linux"):
        result = _uninstall_linux()
    elif os.name == "nt":
        result = _uninstall_windows()
    else:
        raise ServiceError("Unsupported operating system: {}".format(sys.platform))

    runtime = codex_home / APP_NAME
    (runtime / "service.json").unlink(missing_ok=True)
    if remove_data:
        for name in ("logs", "state.json", "sync.lock"):
            path = runtime / name
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
    result["backups_retained"] = str(runtime / "backups")
    return result
