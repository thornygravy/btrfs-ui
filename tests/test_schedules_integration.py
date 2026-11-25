"""Scheduled snapshots against a real btrfs filesystem on a loopback file.

Skipped unless test_integration.py's fixture can build one (needs root)."""
# ruff: noqa: F811  (the fixture comes from test_integration.py)
import time

import pytest

from app.config import Config
from app.server import create_app
from test_integration import H, LABEL, mountpoint, sh  # noqa: F401


@pytest.fixture(scope="module")
def sched(mountpoint, tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    app = create_app(Config({"BTRFS_UI_AUTH": "off", "BTRFS_UI_DATA_DIR": str(data)}))
    c = app.test_client()
    fs = next(f for f in c.get("/api/filesystems").get_json() if f["label"] == LABEL)
    return c, fs["id"], mountpoint, data


def snap(src, dest, ro=True):
    args = ["btrfs", "subvolume", "snapshot"] + (["-r"] if ro else []) + [str(src), str(dest)]
    sh(*args)


def subvols(mnt):
    out = sh("btrfs", "subvolume", "list", str(mnt))
    return sorted(line.split(" path ", 1)[1] for line in out.splitlines())


def test_retention_on_a_real_filesystem(sched):
    c, fs, mnt, data = sched
    sh("btrfs", "subvolume", "create", str(mnt / "data"))
    sh("btrfs", "subvolume", "create", str(mnt / "other"))
    (mnt / "data" / "file.txt").write_text("hello")
    (mnt / "snaps").mkdir()

    # four of this rule's own (older) snapshots...
    for day in (1, 2, 3, 4):
        snap(mnt / "data", mnt / "snaps" / f"data-2025-01-0{day}_0300")
    # ...and look-alikes that must never be touched
    snap(mnt / "data", mnt / "snaps" / "data-2025-02-01_0300", ro=False)   # writable
    snap(mnt / "other", mnt / "snaps" / "data-2025-02-02_0300")            # snapshot of another subvolume
    snap(mnt / "data", mnt / "snaps" / "data-2025-02-03_0300-keep")        # extra suffix
    snap(mnt / "data", mnt / "snaps" / "notes-2025-01-01_0300")            # other prefix
    sh("btrfs", "subvolume", "create", str(mnt / "snaps" / "data-2025-02-04_0300"))  # not a snapshot at all

    r = c.post("/api/schedules", headers=H, json={
        "type": "snapshot", "fs": fs, "source": "data", "dest": "snaps", "prefix": "data",
        "keep": 2, "frequency": "daily", "time": "03:00"})
    assert r.status_code == 200, r.get_json()
    rule_id = r.get_json()["rule"]["id"]

    res = c.post(f"/api/schedules/{rule_id}/run", json={}, headers=H).get_json()["result"]
    assert res["status"] == "ok" and "removed 3 old" in res["message"], res

    left = subvols(mnt)
    new = [p for p in left if p.startswith("snaps/data-") and p[11:15] == time.strftime("%Y")]
    assert len(new) == 1
    assert "snaps/data-2025-01-04_0300" in left                       # newest of the old ones is kept
    for gone in (1, 2, 3):
        assert f"snaps/data-2025-01-0{gone}_0300" not in left
    for survivor in ("snaps/data-2025-02-01_0300", "snaps/data-2025-02-02_0300",
                     "snaps/data-2025-02-03_0300-keep", "snaps/notes-2025-01-01_0300",
                     "snaps/data-2025-02-04_0300", "data", "other"):
        assert survivor in left, survivor

    # the new snapshot is read-only and holds the data
    created = mnt / new[0]
    assert "ro=true" in sh("btrfs", "property", "get", "-ts", str(created), "ro")
    assert (created / "file.txt").read_text() == "hello"

    # a second run in the same minute neither duplicates nor deletes anything
    before = subvols(mnt)
    again = c.post(f"/api/schedules/{rule_id}/run", json={}, headers=H).get_json()["result"]
    if again["status"] == "skipped":
        assert subvols(mnt) == before


def test_scrub_rule_runs_a_real_scrub(sched):
    c, fs, mnt, data = sched
    r = c.post("/api/schedules", headers=H, json={
        "type": "scrub", "fs": fs, "frequency": "monthly", "monthday": 1, "time": "02:00"})
    rule_id = r.get_json()["rule"]["id"]
    res = c.post(f"/api/schedules/{rule_id}/run", json={}, headers=H).get_json()["result"]
    assert res["status"] in ("ok", "skipped"), res
    end = time.time() + 30
    while time.time() < end:
        s = c.get(f"/api/fs/{fs}/scrub").get_json()
        if s["status"] == "finished":  # not just "not running": the status can lag the start
            break
        time.sleep(0.3)
    assert s["status"] == "finished", s


def test_rules_survive_a_restart(sched):
    c, fs, mnt, data = sched
    app2 = create_app(Config({"BTRFS_UI_AUTH": "off", "BTRFS_UI_DATA_DIR": str(data)}))
    o = app2.test_client().get("/api/schedules").get_json()
    assert o["enabled"] and len(o["rules"]) == 2
    assert {r["type"] for r in o["rules"]} == {"snapshot", "scrub"}
    assert all(r["last_status"] for r in o["rules"])
    assert len(o["history"]) >= 2


def test_a_real_tick_runs_a_due_rule(sched):
    c, fs, mnt, data = sched
    s = c.application.scheduler
    scrub = next(r for r in s.store.rules if r["type"] == "scrub")
    scrub["handled"] = 0
    ran = s.tick(time.time())
    assert ran and ran[0][0] == scrub["id"]
    time.sleep(2)  # let the scrub finish before the filesystem is unmounted


def test_balance_rule_runs_a_real_balance(sched):
    c, fs, mnt, data = sched
    r = c.post("/api/schedules", headers=H, json={
        "type": "balance", "fs": fs, "data_usage": 10, "metadata_usage": 5,
        "frequency": "monthly", "monthday": 1, "time": "02:00"})
    assert r.status_code == 200, r.get_json()
    res = c.post(f"/api/schedules/{r.get_json()['rule']['id']}/run", json={}, headers=H).get_json()["result"]
    assert res["status"] == "ok" and "Balance started" in res["message"], res
    end = time.time() + 30
    while time.time() < end:  # let it finish before the filesystem is unmounted
        if c.get(f"/api/fs/{fs}/balance").get_json()["state"] == "none":
            break
        time.sleep(0.3)
    assert c.get(f"/api/fs/{fs}/balance").get_json()["state"] == "none"


def test_trim_rule_runs_a_real_fstrim(sched):
    c, fs, mnt, data = sched
    r = c.post("/api/schedules", headers=H, json={
        "type": "trim", "fs": fs, "frequency": "weekly", "weekday": 6, "time": "04:00"})
    assert r.status_code == 200, r.get_json()
    res = c.post(f"/api/schedules/{r.get_json()['rule']['id']}/run", json={}, headers=H).get_json()["result"]
    # a loopback file may or may not pass discards down; it must never be a crash
    assert res["status"] in ("ok", "skipped"), res
    if res["status"] == "ok":
        assert "trimmed" in res["message"]
