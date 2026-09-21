# CobraWatch

PC-first baseline & drift monitor. Snapshot what your machine is running *right now* — processes, listening ports, startup/persistence entries — then diff any later moment against that baseline. The fastest way to answer: *"what changed on this PC since last week?"*

## Features

- **Full-system snapshot**: running processes (name, exe, cmdline), listening TCP ports, and persistence entries (XDG autostart, cron, Windows Run keys, Startup folder).
- **Smart diffing**: compares by stable identity (exe path, protocol:port, startup name+command), not ephemeral PIDs — so ordinary reboots don't drown you in noise.
- **Severity-ranked drift**: new listeners are flagged **high**, new startup/persistence entries **medium**, process churn **low**. Added *and* removed items are both listed.
- **Snapshot history**: every snapshot is timestamped under `~/.cobrawatch/snapshots/`; `latest.json` always points at the newest baseline.
- **Script-friendly**: `diff` exits `1` when drift is found, `0` when clean — wire it into cron or CI. JSON export supported.
- **Pure stdlib**: Python 3.10+, zero dependencies. Linux/ChromeOS and Windows aware.

## Requirements

- Python 3.10+

## Run

Record a baseline (do this on a machine you trust):

```bash
python3 cobrawatch.py snapshot
```

Later — after updates, new installs, or anything suspicious — diff:

```bash
python3 cobrawatch.py diff
python3 cobrawatch.py diff --against ~/.cobrawatch/snapshots/snapshot-2026-09-01T10-00-00+00-00.json
python3 cobrawatch.py diff --json drift-report.json
```

Inspect a stored snapshot:

```bash
python3 cobrawatch.py show
```

Use a different state directory (e.g. per-machine baselines on a USB stick):

```bash
python3 cobrawatch.py --state-dir /media/usb/baselines snapshot
```

## Workflow suggestion

1. `snapshot` on a fresh, trusted system.
2. `diff` weekly, or immediately after anything feels off.
3. Investigate `+` lines under `listeners` and `startup` first — that's where persistence hides.
4. Re-`snapshot` after intentional changes (updates, new software) to re-baseline.

## Exit codes (diff)

| Code | Meaning |
| ---- | ------- |
| 0 | No drift |
| 1 | Drift detected |
| 2 | No baseline found / bad arguments |

## Tests

```bash
python3 -m unittest discover -s tests
```
