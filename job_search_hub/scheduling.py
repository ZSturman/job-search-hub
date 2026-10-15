"""Explicit, independent macOS launchd / Linux user-systemd scheduling."""

from __future__ import annotations

import os
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from job_search_hub.storage import app_paths, private_write

LABEL = "com.jobsearchhub.refresh"


def _resolve_command() -> list[str]:
    # 1. Standalone jobhub executable on PATH (e.g. from uv tool install, pipx, or system PATH)
    jobhub_bin = shutil.which("jobhub")
    if jobhub_bin:
        return [jobhub_bin, "run"]
    # 2. Sibling jobhub binary in the same directory as the running Python interpreter
    sibling = Path(sys.executable).parent / "jobhub"
    if sibling.is_file():
        return [str(sibling), "run"]
    # 3. Direct module execution fallback
    return [sys.executable, "-m", "job_search_hub", "run"]


def enable(hours=6, *, preview=False):
    if not 1 <= hours <= 24:
        raise ValueError("Choose an interval between 1 and 24 hours.")
    system = platform.system()
    paths = app_paths()
    if system == "Darwin":
        target = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
        command = _resolve_command()
        env_vars = {}
        if os.environ.get("JOBHUB_HOME"):
            env_vars["JOBHUB_HOME"] = os.environ["JOBHUB_HOME"]
        bin_dir = str(Path(command[0]).parent)
        env_vars["PATH"] = f"{bin_dir}:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

        payload = {
            "Label": LABEL,
            "Comment": "Job Search Hub (job_search_hub) scheduled refresh",
            "ProgramArguments": command,
            "StartInterval": hours * 3600,
            "RunAtLoad": False,
            "WorkingDirectory": str(paths.data),
            "StandardOutPath": str(paths.data / "schedule.log"),
            "StandardErrorPath": str(paths.data / "schedule.log"),
            "EnvironmentVariables": env_vars,
        }
        if preview:
            return {"path": str(target), "contents": plistlib.dumps(payload).decode()}
        paths.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        private_write(target, plistlib.dumps(payload))
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
        result = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)],
            capture_output=True,
            text=True,
        )
    elif system == "Linux":
        folder = Path.home() / ".config/systemd/user"
        command_list = _resolve_command()
        command = " ".join(
            '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'
            for s in command_list
        )
        env = ""
        if os.environ.get("JOBHUB_HOME"):
            env = (
                'Environment="JOBHUB_HOME=' + os.environ["JOBHUB_HOME"].replace('"', '\\"') + '"\n'
            )
        bin_dir = str(Path(command_list[0]).parent)
        env += f'Environment="PATH={bin_dir}:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"\n'
        service = (
            "[Unit]\nDescription=Job Search Hub (job_search_hub) "
            + f"refresh\n[Service]\nType=oneshot\n{env}ExecStart={command}\n"
        )
        timer = (
            "[Unit]\nDescription=Job Search Hub (job_search_hub) refresh "
            + f"timer\n[Timer]\nOnBootSec=5min\nOnUnitActiveSec={hours}"
            + "h\nUnit=job-search-hub.service\n[Install]\nWantedBy=timers.target\n"
        )
        if preview:
            return {"service": service, "timer": timer}
        private_write(folder / "job-search-hub.service", service)
        private_write(folder / "job-search-hub.timer", timer)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, capture_output=True)
        result = subprocess.run(
            ["systemctl", "--user", "enable", "--now", "job-search-hub.timer"],
            capture_output=True,
            text=True,
        )
    else:
        raise ValueError(
            "Scheduling is supported on macOS and Linux. Use jobhub run manually on this platform."
        )
    if result.returncode:
        raise ValueError(
            "Scheduler registration failed; inspect your user service manager."
            " Files were preserved."
        )
    return {"enabled": True, "every_hours": hours}


def disable():
    if platform.system() == "Darwin":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
        (Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist").unlink(missing_ok=True)
    elif platform.system() == "Linux":
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", "job-search-hub.timer"], capture_output=True
        )
        folder = Path.home() / ".config/systemd/user"
        for n in ("job-search-hub.service", "job-search-hub.timer"):
            (folder / n).unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    else:
        raise ValueError("Scheduling is supported on macOS and Linux.")
    return {"enabled": False}
