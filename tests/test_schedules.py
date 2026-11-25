"""Scheduled snapshots and scrubs, with the command runner faked out."""
# ruff: noqa: F811  (the fixtures come from test_api.py)
import json
import time
from datetime import datetime

import pytest

from app import schedules as S
from test_api import H, UUID, Runner, make_client, pool  # noqa: F401

DATA_UUID = "38c54ea6-edaf-6845-b6f5-b39552dca90e"
HOME_UUID = "db6682ff-8b93-6e4e-940f-e02897251ea0"


def dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S" if s.count(":") == 2 else "%Y-%m-%d %H:%M")


def rule(**kw):
    base = {"type": "snapshot", "fs": UUID, "enabled": True, "frequency": "daily",
            "time": "03:00", "source": "@data", "dest": "snaps", "prefix": "data",
            "keep": 3, "id": "abcd1234", "created": 0.0, "handled": 0.0}
    base.update(kw)
    return base


# ---- slot maths ---------------------------------------------------------------

@pytest.mark.parametrize("freq,extra,now,latest,nxt", [
    ("hourly", {"time": "00:15"}, "2026-11-19 10:20", "2026-11-19 10:15", "2026-11-19 11:15"),
    ("hourly", {"time": "00:15"}, "2026-11-19 10:10", "2026-11-19 09:15", "2026-11-19 10:15"),
    ("hourly", {"time": "00:00"}, "2026-11-19 00:00", "2026-11-19 00:00", "2026-11-19 01:00"),
    ("daily", {"time": "03:00"}, "2026-11-19 04:00", "2026-11-19 03:00", "2026-11-20 03:00"),
    ("daily", {"time": "03:00"}, "2026-11-19 02:59", "2026-11-18 03:00", "2026-11-19 03:00"),
    ("daily", {"time": "03:00"}, "2026-03-01 02:00", "2026-02-28 03:00", "2026-03-01 03:00"),
    # 2026-11-19 is a Thursday (weekday 3)
    ("weekly", {"time": "03:00", "weekday": 6}, "2026-11-19 12:00", "2026-11-15 03:00", "2026-11-22 03:00"),
    ("weekly", {"time": "03:00", "weekday": 3}, "2026-11-19 02:00", "2026-11-12 03:00", "2026-11-19 03:00"),
    ("weekly", {"time": "03:00", "weekday": 3}, "2026-11-19 03:00", "2026-11-19 03:00", "2026-11-26 03:00"),
    ("monthly", {"time": "03:00", "monthday": 15}, "2026-11-19 12:00", "2026-11-15 03:00", "2026-12-15 03:00"),
    ("monthly", {"time": "03:00", "monthday": 28}, "2026-11-19 12:00", "2026-10-28 03:00", "2026-11-28 03:00"),
    ("monthly", {"time": "03:00", "monthday": 1}, "2026-01-01 02:00", "2025-12-01 03:00", "2026-01-01 03:00"),
    ("monthly", {"time": "03:00", "monthday": 28}, "2026-12-28 04:00", "2026-12-28 03:00", "2027-01-28 03:00"),
])
def test_slots(freq, extra, now, latest, nxt):
    r = rule(frequency=freq, **extra)
    assert S.latest_slot(r, dt(now)) == dt(latest)
    assert S.next_slot(r, dt(now)) == dt(nxt)


def test_due_only_once_per_slot():
    r = rule(handled=S._epoch(dt("2026-11-18 03:00")))
    assert S.is_due(r, dt("2026-11-19 03:00"))
    assert not S.is_due(r, dt("2026-11-19 02:59"))
    r["handled"] = S._epoch(dt("2026-11-19 03:00"))
    assert not S.is_due(r, dt("2026-11-19 20:00"))


# ---- retention ----------------------------------------------------------------

def sv(path, parent=DATA_UUID, ro=True, **kw):
    d = {"id": abs(hash(path)) % 100000 + 300, "path": path, "uuid": "x-" + path,
         "parent_uuid": parent, "readonly": ro, "default": False, "mounted": False}
    d.update(kw)
    return d


def test_keeps_newest_and_returns_oldest_first():
    subs = [sv(f"snaps/data-2026-11-0{n}_0300") for n in (3, 1, 5, 2, 4)]
    gone = S.select_expired(rule(keep=2), subs, DATA_UUID)
    assert [s["path"] for s in gone] == [f"snaps/data-2026-11-0{n}_0300" for n in (1, 2, 3)]


def test_nothing_expires_under_the_limit():
    subs = [sv(f"snaps/data-2026-11-0{n}_0300") for n in (1, 2)]
    assert S.select_expired(rule(keep=2), subs, DATA_UUID) == []


def test_retention_ignores_everything_that_is_not_its_own():
    mine = [sv(f"snaps/data-2026-11-0{n}_0300") for n in (1, 2)]
    decoys = [
        sv("snaps/data-2026-11-03_0300", ro=False),              # writable
        sv("snaps/data-2026-11-04_0300", parent=HOME_UUID),      # snapshot of something else
        sv("snaps/data-2026-11-05_0300", parent=None),           # not a snapshot at all
        sv("other/data-2026-11-06_0300"),                        # another directory
        sv("snaps/deeper/data-2026-11-07_0300"),                 # nested deeper
        sv("snaps/data-2026-11-08_0300-keep"),                   # name does not fully match
        sv("snaps/data-2026-11-09_0300", default=True),          # default subvolume
        sv("snaps/data-2026-11-10_0300", mounted=True),          # mounted somewhere
        sv("snaps/mydata-2026-11-11_0300"),                      # other prefix
        sv("snaps/data-1"),
    ]
    gone = S.select_expired(rule(keep=1), mine + decoys, DATA_UUID)
    assert [s["path"] for s in gone] == ["snaps/data-2026-11-01_0300"]


def test_retention_does_nothing_without_the_source_uuid():
    subs = [sv(f"snaps/data-2026-11-0{n}_0300") for n in (1, 2, 3)]
    assert S.select_expired(rule(keep=1), subs, None) == []
    assert S.select_expired(rule(keep=1), subs, "") == []


def test_prefix_is_matched_literally():
    subs = [sv(f"snaps/d.ta-2026-11-0{n}_0300") for n in (1, 2)] + [sv("snaps/dxta-2026-11-03_0300")]
    gone = S.select_expired(rule(prefix="d.ta", keep=1), subs, DATA_UUID)
    assert [s["path"] for s in gone] == ["snaps/d.ta-2026-11-01_0300"]


# ---- storage ------------------------------------------------------------------

def test_store_is_off_without_a_data_directory(tmp_path):
    s = S.Store(str(tmp_path / "missing"))
    assert not s.enabled and "does not exist" in s.reason


def test_store_round_trip_and_atomic_file(tmp_path):
    s = S.Store(str(tmp_path))
    s.rules.append(rule())
    s.history.append({"time": 1})
    s.save()
    assert [p.name for p in tmp_path.iterdir()] == ["schedules.json"]
    again = S.Store(str(tmp_path))
    assert again.enabled and again.rules == [rule()] and again.history == [{"time": 1}]


@pytest.mark.parametrize("content", ["{not json", "[]", '{"rules": 5}', '{"rules": [{"type": "scrub"}]}',
                                     json.dumps({"rules": [dict(rule(), keep=0)]}),
                                     json.dumps({"rules": [dict(rule(), prefix="../x")]}),
                                     json.dumps({"rules": [dict(rule(), time="25:00")]})])
def test_damaged_file_disables_scheduling_and_is_never_overwritten(tmp_path, content):
    (tmp_path / "schedules.json").write_text(content)
    s = S.Store(str(tmp_path))
    assert not s.enabled and "schedules.json" in s.reason
    assert (tmp_path / "schedules.json").read_text() == content


# ---- through the API ----------------------------------------------------------

class SchedRunner(Runner):
    """Adds timestamp-named snapshots of @data to the fixture listing."""

    def __init__(self):
        super().__init__()
        self.extra = []      # (id, path, parent_uuid)
        self.readonly = set()
        self.deleted = []
        self.created = []
        self.scrub_running = False
        self.balance_text = None   # what `btrfs balance status` prints, when a test wants a balance
        self.trim = ("/mnt/tank: 1.2 GiB (1288490188 bytes) trimmed on /dev/loop0\n", None)  # (output, error)

    def add(self, n, name, parent=DATA_UUID, ro=True):
        self.extra.append((n, name, parent))
        if ro:
            self.readonly.add(n)

    def run(self, args, timeout=60, check=True):
        if args[:3] == ["btrfs", "subvolume", "list"]:
            rc, out, err = super().run(args, timeout, check)
            flags = args[3:-1]
            if flags == ["-r"]:
                out += "".join(f"ID {n} gen 1 top level 5 path {p}\n"
                               for n, p, _ in self.extra if n in self.readonly)
            elif flags == ["-s"]:
                out += "".join(f"ID {n} gen 1 cgen 1 top level 5 otime 2026-11-01 03:00:00 path {p}\n"
                               for n, p, _ in self.extra)
            else:
                out += "".join(
                    f"ID {n} gen 1 cgen 1 parent 5 top level 5 parent_uuid {par} "
                    f"received_uuid - uuid u-{n} path {p}\n" for n, p, par in self.extra)
            return rc, out, err
        if args[:3] == ["btrfs", "subvolume", "snapshot"]:
            self.created.append(args)
            self.calls.append(list(args))
            return 0, "", ""
        if args[:3] == ["btrfs", "subvolume", "delete"]:
            self.deleted.append(int(args[4]))
            self.calls.append(list(args))
            return 0, "", ""
        if args[:3] == ["btrfs", "balance", "status"] and self.balance_text:
            self.calls.append(list(args))
            return 0, self.balance_text, ""
        if args[0] == "fstrim":
            from app import proc
            self.calls.append(list(args))
            if self.trim[1]:
                raise proc.CmdError(self.trim[1], 1)
            return 0, self.trim[0], ""
        if args[:3] == ["btrfs", "scrub", "status"] and self.scrub_running:
            self.calls.append(list(args))
            return 0, "UUID: x\nScrub started:    Sat Nov 14 11:34:19 2026\nStatus:           running\n", ""
        return super().run(args, timeout, check)


@pytest.fixture
def srunner(monkeypatch):
    r = SchedRunner()
    from app import proc, snapper as snapper_mod
    monkeypatch.setattr(proc, "run", r.run)
    monkeypatch.setattr(proc, "run_detached", r.run_detached)
    monkeypatch.setattr(snapper_mod.Snapper, "available", property(lambda self: True))
    return r


@pytest.fixture
def client(srunner, pool, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    c = make_client(pool, BTRFS_UI_DATA_DIR=str(data))
    c.data_dir = data
    return c


def sched(c):
    return c.application.scheduler


def snapshot_body(**kw):
    b = {"type": "snapshot", "fs": UUID, "source": "@data", "dest": "snaps", "prefix": "data",
         "keep": 3, "frequency": "daily", "time": "03:00"}
    b.update(kw)
    return b


def create(c, **kw):
    r = c.post("/api/schedules", json=snapshot_body(**kw), headers=H)
    assert r.status_code == 200, r.get_json()
    return r.get_json()["rule"]


def test_overview_when_the_data_volume_is_missing(srunner, pool, tmp_path):
    c = make_client(pool, BTRFS_UI_DATA_DIR=str(tmp_path / "nope"))
    o = c.get("/api/schedules").get_json()
    assert o["enabled"] is False and "does not exist" in o["reason"] and o["rules"] == []
    r = c.post("/api/schedules", json=snapshot_body(), headers=H)
    assert r.status_code == 400 and "not available" in r.get_json()["error"]


def test_create_list_toggle_delete(client):
    r = create(client)
    assert r["enabled"] and r["prefix"] == "data" and len(r["id"]) == 8
    o = client.get("/api/schedules").get_json()
    assert o["enabled"] and len(o["rules"]) == 1 and o["rules"][0]["next"] > time.time()
    saved = json.loads((client.data_dir / "schedules.json").read_text())
    assert saved["rules"][0]["id"] == r["id"]

    off = client.post(f"/api/schedules/{r['id']}/enabled", json={"enabled": False}, headers=H)
    assert off.get_json()["rule"]["enabled"] is False
    assert client.get("/api/schedules").get_json()["rules"][0]["next"] is None
    assert client.post(f"/api/schedules/{r['id']}/enabled", json={"enabled": "yes"},
                       headers=H).status_code == 400

    assert client.post(f"/api/schedules/{r['id']}/delete", json={}, headers=H).status_code == 200
    assert client.get("/api/schedules").get_json()["rules"] == []
    assert client.post(f"/api/schedules/{r['id']}/delete", json={}, headers=H).status_code == 404


def test_default_prefix_comes_from_the_source(client):
    assert create(client, prefix="", source="@home", dest="snaps")["prefix"] == "@home".lstrip("@")
    assert create(client, prefix="", source="@data/.snapshots", dest="snaps")["prefix"] == "snapshots"


@pytest.mark.parametrize("change,message", [
    ({"type": "reboot"}, "Type"),
    ({"fs": "nope"}, "Unknown filesystem"),
    ({"frequency": "yearly"}, "Frequency"),
    ({"time": "3am"}, "Time"),
    ({"time": "24:00"}, "Time"),
    ({"frequency": "weekly", "weekday": 7}, "weekday"),
    ({"frequency": "weekly"}, "weekday"),
    ({"frequency": "monthly", "monthday": 31}, "day of the month"),
    ({"frequency": "hourly", "minute": 60}, "minute"),
    ({"source": "nonexistent"}, "not a subvolume"),
    ({"source": "../etc"}, "Path may not contain"),
    ({"dest": "missing-dir"}, "No such path"),
    ({"dest": "@data/../.."}, "Path may not contain"),
    ({"prefix": "../evil"}, "prefix"),
    ({"prefix": "a b"}, "prefix"),
    ({"keep": 0}, "keep"),
    ({"keep": "lots"}, "keep"),
    ({"keep": True}, "keep"),
    ({"keep": 1001}, "keep"),
])
def test_rejects_bad_rules(client, change, message):
    r = client.post("/api/schedules", json=snapshot_body(**change), headers=H)
    assert r.status_code in (400, 404), r.get_json()
    assert message in r.get_json()["error"]
    assert client.get("/api/schedules").get_json()["rules"] == []


def test_same_source_dest_and_prefix_cannot_be_added_twice(client):
    create(client)
    r = client.post("/api/schedules", json=snapshot_body(frequency="hourly", minute=5), headers=H)
    assert r.status_code == 400 and "already exists" in r.get_json()["error"]
    create(client, prefix="other")


def test_scrub_rule_needs_no_source(client):
    r = client.post("/api/schedules", json={"type": "scrub", "fs": UUID, "frequency": "monthly",
                                            "monthday": 1, "time": "02:00"}, headers=H)
    assert r.status_code == 200 and "source" not in r.get_json()["rule"]


def test_rule_count_is_capped(client, monkeypatch):
    monkeypatch.setattr(S, "MAX_RULES", 2)
    create(client, prefix="a"); create(client, prefix="b")
    assert client.post("/api/schedules", json=snapshot_body(prefix="c"), headers=H).status_code == 400


def test_read_only_mode_blocks_every_change_and_never_runs(srunner, pool, tmp_path):
    (tmp_path / "d").mkdir()
    c = make_client(pool, BTRFS_UI_DATA_DIR=str(tmp_path / "d"), BTRFS_UI_READONLY="1")
    assert c.get("/api/schedules").get_json()["readonly"] is True
    for path in ("/api/schedules", "/api/schedules/x/enabled", "/api/schedules/x/run",
                 "/api/schedules/x/delete"):
        assert c.post(path, json={}, headers=H).status_code == 403
    assert c.application.scheduler.tick(time.time() + 10 ** 7) == []


def test_changes_need_the_csrf_header(client):
    assert client.post("/api/schedules", json=snapshot_body()).status_code == 400


def test_snapshot_runs_when_due_and_only_once(client, srunner):
    r = create(client)
    s = sched(client)
    s.store.rules[0]["handled"] = 0  # pretend it was created long ago
    now = S._epoch(dt("2026-11-19 03:00:30"))
    assert s.tick(now) == [(r["id"], "ok")]
    assert len(srunner.created) == 1
    args = srunner.created[0]
    assert args[:4] == ["btrfs", "subvolume", "snapshot", "-r"]
    assert args[4].endswith("/@data") and args[5].endswith("/snaps/data-2026-11-19_0300")
    assert s.tick(now + 60) == []  # same slot is not run again
    assert len(srunner.created) == 1
    hist = client.get("/api/schedules").get_json()["history"]
    assert hist[0]["status"] == "ok" and hist[0]["trigger"] == "schedule"
    assert "data-2026-11-19_0300" in hist[0]["message"]
    again = S.Store(str(client.data_dir))
    assert again.rules[0]["last_status"] == "ok" and again.rules[0]["handled"] > 0


def test_nothing_runs_before_the_first_slot(client, srunner):
    create(client)
    assert sched(client).tick() == []  # created just now, next slot is in the future
    assert srunner.created == []


def test_missed_runs_are_caught_up_once(client, srunner):
    create(client)
    sched(client).store.rules[0]["handled"] = 0
    assert len(sched(client).tick(S._epoch(dt("2026-11-25 12:00")))) == 1
    assert len(srunner.created) == 1
    assert srunner.created[0][5].endswith("data-2026-11-25_1200")  # named for when it really ran


def test_re_enabling_does_not_replay_the_time_it_was_off(client, srunner):
    r = create(client)
    client.post(f"/api/schedules/{r['id']}/enabled", json={"enabled": False}, headers=H)
    sched(client).store.rules[0]["handled"] = 0
    client.post(f"/api/schedules/{r['id']}/enabled", json={"enabled": True}, headers=H)
    assert sched(client).store.rules[0]["handled"] > time.time() - 5
    assert sched(client).tick() == []


def test_disabled_rules_do_not_run(client, srunner):
    r = create(client)
    client.post(f"/api/schedules/{r['id']}/enabled", json={"enabled": False}, headers=H)
    sched(client).store.rules[0]["handled"] = 0
    assert sched(client).tick(S._epoch(dt("2026-11-19 03:00:30"))) == []


def test_retention_deletes_only_what_it_owns(client, srunner):
    srunner.add(401, "snaps/data-2026-11-17_0300")
    srunner.add(402, "snaps/data-2026-11-18_0300")
    srunner.add(403, "snaps/data-2026-11-19_0300")        # same minute: the new one already "exists" in the listing
    srunner.add(404, "snaps/data-2026-11-16_0300")
    srunner.add(405, "snaps/data-2026-11-15_0300", ro=False)           # writable: untouched
    srunner.add(406, "snaps/data-2026-11-14_0300", parent=HOME_UUID)   # someone else's: untouched
    create(client, keep=2)
    sched(client).store.rules[0]["handled"] = 0
    # a different minute than the listing, so a new snapshot is really created
    assert sched(client).tick(S._epoch(dt("2026-11-20 03:00:30"))) == [(sched(client).store.rules[0]["id"], "ok")]
    # the fake listing does not grow, so 4 of mine exist and keep=2 removes the two oldest
    assert sorted(srunner.deleted) == [401, 404]
    assert all(c[:3] == ["btrfs", "subvolume", "delete"] for c in srunner.calls if c[2] == "delete")
    assert 405 not in srunner.deleted and 406 not in srunner.deleted
    hist = client.get("/api/schedules").get_json()["history"][0]
    assert "removed 2 old" in hist["message"]


def test_a_failed_delete_is_reported_but_the_rest_continue(client, srunner, monkeypatch):
    from app import proc
    srunner.add(401, "snaps/data-2026-11-10_0300")
    srunner.add(402, "snaps/data-2026-11-11_0300")
    srunner.add(403, "snaps/data-2026-11-12_0300")
    base = srunner.run

    def flaky(args, timeout=60, check=True):
        if args[:3] == ["btrfs", "subvolume", "delete"] and args[4] == "401":
            raise proc.CmdError("busy")
        return base(args, timeout, check)

    monkeypatch.setattr(proc, "run", flaky)
    create(client, keep=1)
    entry = sched(client).run_now(sched(client).store.rules[0]["id"])
    assert entry["status"] == "error" and "busy" in entry["message"]
    assert srunner.deleted == [402]


def test_existing_target_is_skipped_not_overwritten(client, srunner):
    create(client)
    (client.application.scheduler.registry.get(UUID).primary.mount_point and None)
    mp = sched(client).registry.get(UUID).primary.mount_point
    stamp = datetime.now().strftime(S.STAMP)
    import os
    os.mkdir(os.path.join(mp, "snaps", f"data-{stamp}"))
    entry = sched(client).run_now(sched(client).store.rules[0]["id"])
    assert entry["status"] == "skipped" and srunner.created == []


def test_missing_source_is_an_error_not_a_crash(client, srunner):
    create(client)
    sched(client).store.rules[0]["source"] = "gone"
    entry = sched(client).run_now(sched(client).store.rules[0]["id"])
    assert entry["status"] == "error" and "no longer exists" in entry["message"]
    assert srunner.created == []


def test_unmounted_filesystem_is_skipped(client, srunner):
    create(client)
    sched(client).store.rules[0]["fs"] = "00000000-0000-0000-0000-000000000000"
    entry = sched(client).run_now(sched(client).store.rules[0]["id"])
    assert entry["status"] == "skipped" and "not mounted" in entry["message"]


def test_run_now_endpoint_and_history(client, srunner):
    r = create(client)
    res = client.post(f"/api/schedules/{r['id']}/run", json={}, headers=H).get_json()["result"]
    assert res["status"] == "ok" and res["trigger"] == "manual"
    assert client.post("/api/schedules/nope/run", json={}, headers=H).status_code == 404
    assert client.get("/api/schedules").get_json()["history"][0]["trigger"] == "manual"


def test_scrub_rule_starts_a_scrub(client, srunner):
    r = client.post("/api/schedules", json={"type": "scrub", "fs": UUID, "frequency": "weekly",
                                            "weekday": 6, "time": "02:00"}, headers=H).get_json()["rule"]
    res = client.post(f"/api/schedules/{r['id']}/run", json={}, headers=H).get_json()["result"]
    assert res["status"] == "ok" and res["message"] == "Scrub started"
    assert len(srunner.detached) == 1 and srunner.detached[0][:3] == ["btrfs", "scrub", "start"]


def test_scrub_rule_skips_when_one_is_running(client, srunner):
    r = client.post("/api/schedules", json={"type": "scrub", "fs": UUID, "frequency": "weekly",
                                            "weekday": 6, "time": "02:00"}, headers=H).get_json()["rule"]
    srunner.scrub_running = True
    res = client.post(f"/api/schedules/{r['id']}/run", json={}, headers=H).get_json()["result"]
    assert res["status"] == "skipped" and srunner.detached == []


def new_rule(c, typ, **kw):
    body = {"type": typ, "fs": UUID, "frequency": "monthly", "monthday": 1, "time": "02:00", **kw}
    return c.post("/api/schedules", json=body, headers=H)


def run_now(c, rule_id):
    return c.post(f"/api/schedules/{rule_id}/run", json={}, headers=H).get_json()["result"]


# ---- balance ------------------------------------------------------------------

def test_balance_rule_starts_a_gentle_balance(client, srunner):
    r = new_rule(client, "balance", data_usage=10, metadata_usage=5)
    assert r.status_code == 200
    rule_ = r.get_json()["rule"]
    assert rule_["data_usage"] == 10 and rule_["metadata_usage"] == 5
    res = run_now(client, rule_["id"])
    assert res["status"] == "ok" and res["message"] == "Balance started (data under 10%, metadata under 5%)"
    cmd = srunner.detached[0]
    assert cmd[:4] == ["btrfs", "balance", "start", "--background"]
    assert cmd[4:6] == ["-dusage=10", "-musage=5"] and "--full-balance" not in cmd


def test_balance_can_cover_just_one_kind_of_chunk(client, srunner):
    only_data = new_rule(client, "balance", data_usage="20", metadata_usage="").get_json()["rule"]
    assert only_data["metadata_usage"] is None
    run_now(client, only_data["id"])
    assert srunner.detached[-1][4:-1] == ["-dusage=20"]
    only_meta = new_rule(client, "balance", metadata_usage=0).get_json()["rule"]
    assert only_meta["data_usage"] is None and only_meta["metadata_usage"] == 0
    run_now(client, only_meta["id"])
    assert srunner.detached[-1][4:-1] == ["-musage=0"]


@pytest.mark.parametrize("change,message", [
    ({}, "usage percentage"),
    ({"data_usage": "", "metadata_usage": ""}, "usage percentage"),
    ({"data_usage": 101}, "data usage"),
    ({"data_usage": -1}, "data usage"),
    ({"metadata_usage": "lots"}, "metadata usage"),
    ({"data_usage": True}, "data usage"),
])
def test_balance_rule_needs_sensible_percentages(client, change, message):
    r = new_rule(client, "balance", **change)
    assert r.status_code == 400 and message in r.get_json()["error"]
    assert client.get("/api/schedules").get_json()["rules"] == []


@pytest.mark.parametrize("text,word", [
    ("Balance on '/mnt/tank' is running\n1 out of about 5 chunks balanced\n", "running"),
    ("Balance on '/mnt/tank' is paused\n", "paused"),
    ("Balance on '/mnt/tank' is running, cancel requested\n", "cancelling"),
])
def test_balance_is_skipped_while_another_balance_exists(client, srunner, text, word):
    rid = new_rule(client, "balance", data_usage=10).get_json()["rule"]["id"]
    srunner.balance_text = text
    res = run_now(client, rid)
    assert res["status"] == "skipped" and word in res["message"] and srunner.detached == []


def test_balance_is_skipped_while_a_scrub_runs(client, srunner):
    rid = new_rule(client, "balance", data_usage=10).get_json()["rule"]["id"]
    srunner.scrub_running = True
    res = run_now(client, rid)
    assert res["status"] == "skipped" and "scrub" in res["message"] and srunner.detached == []


def test_a_balance_rule_runs_when_due_and_only_once(client, srunner):
    rid = new_rule(client, "balance", data_usage=10, frequency="daily", time="03:00").get_json()["rule"]["id"]
    s = sched(client)
    s.store.rules[0]["handled"] = S._epoch(dt("2026-11-18 03:00"))
    now = S._epoch(dt("2026-11-19 03:05"))
    assert s.tick(now) == [(rid, "ok")]
    assert s.tick(now + 60) == []
    assert len(srunner.detached) == 1


# ---- trim ---------------------------------------------------------------------

def test_trim_rule_runs_fstrim_and_reports_what_it_freed(client, srunner):
    r = new_rule(client, "trim", frequency="weekly", weekday=6)
    assert r.status_code == 200
    rule_ = r.get_json()["rule"]
    assert "data_usage" not in rule_ and "source" not in rule_
    res = run_now(client, rule_["id"])
    assert res["status"] == "ok" and res["message"] == "/mnt/tank: 1.2 GiB (1288490188 bytes) trimmed on /dev/loop0"
    trims = [c for c in srunner.calls if c[0] == "fstrim"]
    assert len(trims) == 1 and trims[0][1] == "-v" and trims[0][2].endswith("pool")


def test_trim_with_no_output_still_says_it_finished(client, srunner):
    srunner.trim = ("", None)
    rid = new_rule(client, "trim").get_json()["rule"]["id"]
    assert run_now(client, rid)["message"] == "Trim finished"


def test_trim_on_devices_without_discard_is_skipped_not_an_error(client, srunner):
    srunner.trim = ("", "fstrim: /mnt/tank: the discard operation is not supported")
    rid = new_rule(client, "trim").get_json()["rule"]["id"]
    res = run_now(client, rid)
    assert res["status"] == "skipped" and "do not support trim" in res["message"]


def test_a_failed_trim_is_an_error_with_the_reason(client, srunner):
    srunner.trim = ("", "fstrim: /mnt/tank: FITRIM ioctl failed: Operation not permitted")
    rid = new_rule(client, "trim").get_json()["rule"]["id"]
    res = run_now(client, rid)
    assert res["status"] == "error" and "Operation not permitted" in res["message"]


def test_rule_names_in_the_history(client, srunner):
    for typ, extra in (("scrub", {}), ("balance", {"data_usage": 10}), ("trim", {})):
        run_now(client, new_rule(client, typ, **extra).get_json()["rule"]["id"])
    whats = [e["what"] for e in client.get("/api/schedules").get_json()["history"]]
    assert whats == ["Trim", "Balance", "Scrub"]


def test_unknown_types_are_still_refused(client):
    r = new_rule(client, "defrag")
    assert r.status_code == 400 and "balance" in r.get_json()["error"]


def test_a_stored_balance_rule_is_checked_when_loaded(tmp_path):
    good = dict(rule(type="balance"), data_usage=10, metadata_usage=None)
    (tmp_path / "schedules.json").write_text(json.dumps({"rules": [good]}))
    assert S.Store(str(tmp_path)).enabled
    for bad in (dict(good, data_usage=None), dict(good, data_usage=101), dict(good, data_usage=True),
                dict(good, data_usage="10")):
        (tmp_path / "schedules.json").write_text(json.dumps({"rules": [bad]}))
        assert not S.Store(str(tmp_path)).enabled


def test_history_is_trimmed(client, srunner, monkeypatch):
    monkeypatch.setattr(S, "HISTORY_LIMIT", 5)
    r = client.post("/api/schedules", json={"type": "scrub", "fs": UUID, "frequency": "weekly",
                                            "weekday": 6, "time": "02:00"}, headers=H).get_json()["rule"]
    for _ in range(8):
        client.post(f"/api/schedules/{r['id']}/run", json={}, headers=H)
    saved = json.loads((client.data_dir / "schedules.json").read_text())
    assert len(saved["history"]) == 5


def test_the_background_thread_only_starts_when_it_can_work(srunner, pool, tmp_path):
    c = make_client(pool, BTRFS_UI_DATA_DIR=str(tmp_path / "nope"))
    c.application.scheduler.start()
    assert c.application.scheduler._thread is None
