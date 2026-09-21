import json
import tempfile
import unittest
from pathlib import Path

from cobrawatch import (
    diff_snapshots,
    drift_severity,
    load_snapshot,
    save_snapshot,
    take_snapshot,
)


def _snapshot(processes, listeners, startup):
    return {
        "created": "2026-09-21T00:00:00+00:00",
        "platform": "test",
        "processes": processes,
        "listeners": listeners,
        "startup": startup,
    }


class DiffTests(unittest.TestCase):
    def test_identical_snapshots_show_no_drift(self) -> None:
        snap = _snapshot(
            processes=[{"name": "init", "exe": "/sbin/init", "cmdline": ""}],
            listeners=[{"protocol": "tcp4", "port": 22}],
            startup=[{"source": "/etc/xdg/autostart", "name": "app", "command": "app"}],
        )
        drifts = diff_snapshots(snap, dict(snap))
        self.assertTrue(all(not d.changed for d in drifts))

    def test_new_listener_is_high_severity(self) -> None:
        baseline = _snapshot([], [{"protocol": "tcp4", "port": 22}], [])
        current = _snapshot(
            [],
            [{"protocol": "tcp4", "port": 22}, {"protocol": "tcp4", "port": 4444}],
            [],
        )
        drifts = {d.category: d for d in diff_snapshots(baseline, current)}
        listener_drift = drifts["listeners"]
        self.assertEqual(listener_drift.added, ["tcp4:4444"])
        self.assertEqual(drift_severity(listener_drift), "high")

    def test_new_startup_entry_is_medium_severity(self) -> None:
        baseline = _snapshot([], [], [])
        current = _snapshot(
            [], [],
            [{"source": "/home/u/.config/autostart", "name": "upd", "command": "/tmp/upd"}],
        )
        drifts = {d.category: d for d in diff_snapshots(baseline, current)}
        startup_drift = drifts["startup"]
        self.assertEqual(len(startup_drift.added), 1)
        self.assertEqual(drift_severity(startup_drift), "medium")

    def test_removed_process_is_low_severity(self) -> None:
        baseline = _snapshot(
            [{"name": "old", "exe": "/usr/bin/old", "cmdline": ""}], [], []
        )
        current = _snapshot([], [], [])
        drifts = {d.category: d for d in diff_snapshots(baseline, current)}
        process_drift = drifts["processes"]
        self.assertEqual(process_drift.removed, ["/usr/bin/old"])
        self.assertEqual(drift_severity(process_drift), "low")

    def test_pid_changes_alone_do_not_count(self) -> None:
        # Processes are keyed by exe/name, so a pid change shows no drift.
        baseline = _snapshot([{"name": "app", "exe": "/opt/app", "cmdline": "a"}], [], [])
        current = _snapshot([{"name": "app", "exe": "/opt/app", "cmdline": "b"}], [], [])
        drifts = diff_snapshots(baseline, current)
        self.assertTrue(all(not d.changed for d in drifts))

    def test_no_drift_severity_is_none(self) -> None:
        from cobrawatch import Drift
        self.assertEqual(drift_severity(Drift(category="processes")), "none")


class SnapshotStorageTests(unittest.TestCase):
    def test_save_and_load_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            snapshot = _snapshot([], [], [])
            path = save_snapshot(snapshot, state_dir)

            self.assertTrue(path.is_file())
            self.assertTrue((state_dir / "latest.json").is_file())
            self.assertEqual(load_snapshot(path), snapshot)
            self.assertEqual(load_snapshot(state_dir / "latest.json"), snapshot)

    def test_take_snapshot_shape(self) -> None:
        snapshot = take_snapshot()
        self.assertIn("created", snapshot)
        self.assertIn("processes", snapshot)
        self.assertIn("listeners", snapshot)
        self.assertIn("startup", snapshot)
        self.assertIsInstance(snapshot["processes"], list)
        # JSON-serializable, since that's how it is stored.
        json.dumps(snapshot)


if __name__ == "__main__":
    unittest.main()
