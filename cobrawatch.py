#!/usr/bin/env python3
"""CobraWatch - PC-first baseline & drift monitor.

Take a snapshot of what is running on this PC right now (processes,
listening ports, startup/persistence entries). Later, `diff` compares the
live system against that baseline and highlights what appeared, changed
or vanished — the fastest way to spot a foothold that wasn't there last week.

Pure standard library; Python 3.10+. Linux/ChromeOS and Windows aware.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

APP_NAME = "CobraWatch"
STATE_DIR = Path.home() / ".cobrawatch"
SNAPSHOT_DIR = STATE_DIR / "snapshots"
LATEST_SNAPSHOT = STATE_DIR / "latest.json"


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------

def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def collect_processes() -> list[dict]:
    """Running processes. Key fields: name, exe, cmdline."""
    if sys.platform.startswith("win"):
        return _collect_processes_windows()
    processes: list[dict] = []
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return processes
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        name = (_read_text(entry / "comm") or "").strip()
        cmdline_raw = _read_text(entry / "cmdline") or ""
        cmdline = cmdline_raw.replace("\x00", " ").strip()
        exe = ""
        try:
            exe = str((entry / "exe").resolve())
        except OSError:
            pass
        if name or cmdline:
            processes.append({"name": name, "exe": exe, "cmdline": cmdline[:200]})
    return processes


def _collect_processes_windows() -> list[dict]:
    try:
        result = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return []
    processes: list[dict] = []
    for row in csv.reader(StringIO(result.stdout)):
        if row:
            processes.append({"name": row[0], "exe": row[0], "cmdline": ""})
    return processes


def collect_listeners() -> list[dict]:
    """Listening TCP sockets: protocol + port (+ pid on POSIX)."""
    if sys.platform.startswith("win"):
        return _collect_listeners_windows()
    listeners: list[dict] = []
    for proc_file, protocol in ((Path("/proc/net/tcp"), "tcp4"),
                                (Path("/proc/net/tcp6"), "tcp6")):
        content = _read_text(proc_file)
        if content is None:
            continue
        for line in content.splitlines()[1:]:
            fields = line.split()
            if len(fields) < 4 or fields[3] != "0A":  # 0A == LISTEN
                continue
            _, _, port_hex = fields[1].rpartition(":")
            try:
                port = int(port_hex, 16)
            except ValueError:
                continue
            listeners.append({"protocol": protocol, "port": port})
    return listeners


def _collect_listeners_windows() -> list[dict]:
    try:
        result = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return []
    listeners: list[dict] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0].upper() == "TCP" and "LISTEN" in parts[3].upper():
            _, _, port_text = parts[1].rpartition(":")
            try:
                listeners.append({"protocol": "tcp", "port": int(port_text)})
            except ValueError:
                continue
    return listeners


def collect_startup() -> list[dict]:
    """Persistence entries: autostart .desktop files, cron, Run keys, Startup folder."""
    if sys.platform.startswith("win"):
        return _collect_startup_windows()
    entries: list[dict] = []
    for autostart_dir in (Path.home() / ".config" / "autostart", Path("/etc/xdg/autostart")):
        if not autostart_dir.is_dir():
            continue
        for desktop_file in sorted(autostart_dir.glob("*.desktop")):
            content = _read_text(desktop_file) or ""
            name = desktop_file.stem
            exec_line = ""
            for line in content.splitlines():
                if line.startswith("Name="):
                    name = line.partition("=")[2].strip()
                elif line.startswith("Exec="):
                    exec_line = line.partition("=")[2].strip()
            entries.append({"source": str(autostart_dir), "name": name, "command": exec_line})
    for cron_base in (Path("/etc/crontab"), Path("/etc/cron.d")):
        if cron_base.is_file():
            entries.append({"source": str(cron_base), "name": cron_base.name, "command": ""})
        elif cron_base.is_dir():
            for cron_file in sorted(cron_base.iterdir()):
                if cron_file.is_file():
                    entries.append({"source": str(cron_base), "name": cron_file.name, "command": ""})
    return entries


def _collect_startup_windows() -> list[dict]:
    entries: list[dict] = []
    try:
        import winreg  # type: ignore
    except ImportError:
        winreg = None
    if winreg is not None:
        for hive, hive_name in ((winreg.HKEY_CURRENT_USER, "HKCU"),
                                (winreg.HKEY_LOCAL_MACHINE, "HKLM")):
            subkey = r"Software\Microsoft\Windows\CurrentVersion\Run"
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    index = 0
                    while True:
                        try:
                            name, value, _ = winreg.EnumValue(key, index)
                        except OSError:
                            break
                        entries.append({
                            "source": f"{hive_name}\\{subkey}",
                            "name": name,
                            "command": str(value),
                        })
                        index += 1
            except OSError:
                continue
    startup_dir = (
        Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows"
        / "Start Menu" / "Programs" / "Startup"
    )
    if startup_dir.is_dir():
        for item in sorted(startup_dir.iterdir()):
            entries.append({"source": str(startup_dir), "name": item.name, "command": str(item)})
    return entries


def take_snapshot() -> dict:
    return {
        "created": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "platform": sys.platform,
        "processes": collect_processes(),
        "listeners": collect_listeners(),
        "startup": collect_startup(),
    }


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------

@dataclass
class Drift:
    """One category of change between baseline and now."""
    category: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)


def _identity_keys(snapshot: dict) -> dict[str, set[str]]:
    """Reduce each category to stable identity sets (pids are ephemeral)."""
    processes = {
        (p.get("exe") or p.get("name") or "").lower()
        for p in snapshot.get("processes", [])
    } - {""}
    listeners = {
        f"{l.get('protocol', 'tcp')}:{l.get('port')}"
        for l in snapshot.get("listeners", [])
    }
    startup = {
        f"{s.get('source', '')}::{s.get('name', '')}::{s.get('command', '')}"
        for s in snapshot.get("startup", [])
    }
    return {"processes": processes, "listeners": listeners, "startup": startup}


def diff_snapshots(baseline: dict, current: dict) -> list[Drift]:
    base_keys = _identity_keys(baseline)
    current_keys = _identity_keys(current)
    drifts: list[Drift] = []
    for category in ("processes", "listeners", "startup"):
        drifts.append(Drift(
            category=category,
            added=sorted(current_keys[category] - base_keys[category]),
            removed=sorted(base_keys[category] - current_keys[category]),
        ))
    return drifts


def drift_severity(drift: Drift) -> str:
    """New listeners and startup entries matter most; process churn is noise-ish."""
    if drift.category == "listeners" and drift.added:
        return "high"
    if drift.category == "startup" and drift.added:
        return "medium"
    if drift.changed:
        return "low"
    return "none"


def save_snapshot(snapshot: dict, state_dir: Path = STATE_DIR) -> Path:
    snapshot_dir = state_dir / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    stamp = snapshot["created"].replace(":", "-")
    path = snapshot_dir / f"snapshot-{stamp}.json"
    path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    (state_dir / "latest.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    return path


def load_snapshot(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Could not load snapshot {path}: {exc}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_snapshot(args: argparse.Namespace) -> int:
    snapshot = take_snapshot()
    path = save_snapshot(snapshot, Path(args.state_dir) if args.state_dir else STATE_DIR)
    print(f"Snapshot saved: {path}")
    print(f"  processes: {len(snapshot['processes'])}")
    print(f"  listeners: {len(snapshot['listeners'])}")
    print(f"  startup entries: {len(snapshot['startup'])}")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    baseline_path = Path(args.against) if args.against else (
        Path(args.state_dir) / "latest.json" if args.state_dir else LATEST_SNAPSHOT
    )
    if not baseline_path.is_file():
        print(f"No baseline snapshot at {baseline_path}.", file=sys.stderr)
        print("Create one first: cobrawatch.py snapshot", file=sys.stderr)
        return 2

    baseline = load_snapshot(baseline_path)
    current = take_snapshot()
    drifts = diff_snapshots(baseline, current)

    print(f"Baseline: {baseline_path} ({baseline.get('created', 'unknown time')})")
    print(f"Now:      {current['created']}\n")

    any_changes = False
    for drift in drifts:
        severity = drift_severity(drift)
        if not drift.changed:
            print(f"[--  ] {drift.category}: no changes")
            continue
        any_changes = True
        badge = {"high": "[HIGH]", "medium": "[MED ]", "low": "[LOW ]"}[severity]
        print(f"{badge} {drift.category}: +{len(drift.added)} / -{len(drift.removed)}")
        for item in drift.added[:20]:
            print(f"       + {item}")
        if len(drift.added) > 20:
            print(f"       ... and {len(drift.added) - 20} more added")
        for item in drift.removed[:20]:
            print(f"       - {item}")
        if len(drift.removed) > 20:
            print(f"       ... and {len(drift.removed) - 20} more removed")

    if args.json:
        payload = {
            "baseline": str(baseline_path),
            "created": current["created"],
            "drifts": [
                {"category": d.category, "added": d.added, "removed": d.removed,
                 "severity": drift_severity(d)}
                for d in drifts
            ],
        }
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nJSON report written to {out}")

    if not any_changes:
        print("\nNo drift detected — system matches the baseline.")
        return 0
    print("\nDrift detected. New listeners or startup entries deserve a look.")
    return 1


def cmd_show(args: argparse.Namespace) -> int:
    path = Path(args.snapshot) if args.snapshot else LATEST_SNAPSHOT
    snapshot = load_snapshot(path)
    print(f"Snapshot {path} ({snapshot.get('created', 'unknown time')})")
    print(f"\nListeners ({len(snapshot.get('listeners', []))}):")
    for listener in sorted({f"{l.get('protocol')}:{l.get('port')}"
                            for l in snapshot.get("listeners", [])}):
        print(f"  {listener}")
    print(f"\nStartup entries ({len(snapshot.get('startup', []))}):")
    for entry in snapshot.get("startup", []):
        print(f"  {entry.get('name')}  [{entry.get('source')}]")
    print(f"\nProcesses ({len(snapshot.get('processes', []))}): (showing first 30)")
    for proc in snapshot.get("processes", [])[:30]:
        print(f"  {proc.get('name')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME.lower(),
        description="Baseline & drift monitor: snapshot the PC now, diff it later.",
    )
    parser.add_argument("--state-dir", help="Override the ~/.cobrawatch state directory.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot_parser = subparsers.add_parser("snapshot", help="Record a baseline snapshot.")
    snapshot_parser.set_defaults(handler=cmd_snapshot)

    diff_parser = subparsers.add_parser("diff", help="Compare the live system to a snapshot.")
    diff_parser.add_argument("--against", help="Snapshot file to diff against "
                                               "(default: latest).")
    diff_parser.add_argument("--json", help="Write a JSON drift report to this path.")
    diff_parser.set_defaults(handler=cmd_diff)

    show_parser = subparsers.add_parser("show", help="Print the contents of a snapshot.")
    show_parser.add_argument("snapshot", nargs="?", help="Snapshot file (default: latest).")
    show_parser.set_defaults(handler=cmd_show)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
