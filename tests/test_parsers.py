"""Parsers, checked against output captured from real btrfs-progs 6.6.3 and
snapper 0.10.6 (see tests/fixtures)."""
from app import btrfs, snapper
from conftest import fixture


def test_show_raw():
    s = btrfs.parse_show(fixture("show_raw.txt"))
    assert s["label"] == "testpool"
    assert s["uuid"] == "81182bc3-6b88-4f7b-83b7-33f99a1f706e"
    assert [d["devid"] for d in s["devices"]] == [1, 2]
    assert s["devices"][0]["path"] == "/dev/loop0"
    assert s["devices"][0]["size"] == 629145600
    assert not s["devices"][0]["missing"]


def test_show_without_dev_nodes_reports_missing():
    # What btrfs prints inside a container that has no block device nodes.
    s = btrfs.parse_show(fixture("show_nodev.txt"))
    assert all(d["missing"] and d["size"] == 0 for d in s["devices"])


def test_show_label_none_and_spaces():
    s = btrfs.parse_show("Label: none  uuid: 11111111-2222-3333-4444-555555555555\n")
    assert s["label"] == "" and s["uuid"].startswith("1111")
    s = btrfs.parse_show("Label: 'my big pool'  uuid: 11111111-2222-3333-4444-555555555555\n")
    assert s["label"] == "my big pool"


def test_df():
    rows = btrfs.parse_df(fixture("df_b.txt"))
    assert [r["type"] for r in rows] == ["Data", "System", "Metadata", "GlobalReserve"]
    assert rows[0] == {"type": "Data", "profile": "RAID1", "total": 268435456, "used": 41943040}


def test_df_multiple_profiles_and_mixed():
    rows = btrfs.parse_df("Data, single: total=1, used=1\nData+Metadata, DUP (multiple profiles): total=2, used=1\n")
    assert rows[1]["type"] == "Data+Metadata" and rows[1]["profile"] == "DUP"


def test_usage_overall():
    u = btrfs.parse_usage(fixture("usage_b.txt"))
    assert u["device_size"] == 1258291200
    assert u["used"] == 84639744
    assert u["free_estimated"] == 419430400 and u["free_estimated_min"] == 419430400
    assert u["data_ratio"] == 2.0
    assert u["multiple_profiles"] == "no"


def test_usage_devices():
    d = btrfs.parse_usage_devices(fixture("usage_b.txt"))
    assert set(d) == {"/dev/loop0", "/dev/loop1"}
    # allocated = data + metadata + system on that device; size = allocated + unallocated
    assert d["/dev/loop0"]["allocated"] == 268435456 + 134217728 + 33554432
    assert d["/dev/loop0"]["allocated"] + d["/dev/loop0"]["unallocated"] == 629145600


def test_device_stats():
    s = btrfs.parse_device_stats(fixture("device_stats.txt"))
    assert s["/dev/loop0"]["write_io_errs"] == 0
    assert len(s) == 2 and len(s["/dev/loop1"]) == 5
    s = btrfs.parse_device_stats("[/dev/sda1].corruption_errs  3\n")
    assert s["/dev/sda1"]["corruption_errs"] == 3


def test_subvolume_list_full():
    items = btrfs.parse_subvol_list(fixture("subvol_list_full.txt"))
    by_path = {i["path"]: i for i in items}
    data = by_path["@data"]
    assert data["id"] == 256 and data["parent"] == 5 and data["top_level"] == 5
    assert data["parent_uuid"] is None and data["uuid"].startswith("38c54ea6")
    snap = by_path["snaps/data-1"]
    assert snap["parent_uuid"] == data["uuid"]


def test_subvolume_list_snapshots_has_otime():
    items = btrfs.parse_subvol_list(fixture("subvol_list_s.txt"))
    assert items[0]["otime"] == "2026-11-14 11:26:19"


def test_subvolume_list_path_with_spaces_and_path_word():
    items = btrfs.parse_subvol_list(
        "ID 300 gen 5 cgen 5 parent 5 top level 5 parent_uuid - received_uuid - uuid abc path my path stuff/x\n")
    assert items[0]["path"] == "my path stuff/x"


def test_subvolume_list_readonly_ids():
    assert [s["id"] for s in btrfs.parse_subvol_list(fixture("subvol_list_r.txt"))] == [258, 259, 261, 262, 263]


def test_default_id():
    assert btrfs.parse_default_id("ID 5 (FS_TREE)\n") == 5
    assert btrfs.parse_default_id("ID 256 gen 8 top level 5 path @\n") == 256


def test_qgroups():
    q = btrfs.parse_qgroups(fixture("qgroup_show.txt"))
    assert q[258] == {"referenced": 20987904, "exclusive": 16384}
    assert 5 in q  # top level
    assert btrfs.parse_qgroups("ERROR: can't list qgroups: quotas not enabled\n") == {}


def test_scrub_finished():
    s = btrfs.scrub_summary(fixture("scrub_status.txt"))
    assert s["status"] == "finished" and not s["running"]
    assert ["Error summary", "no errors found"] in s["pairs"]


def test_scrub_never_run():
    s = btrfs.scrub_summary("UUID:             x\n\tno stats available\nTotal to scrub:   1GiB\n")
    assert s["status"] == "never run"


def test_scrub_running():
    # Layout as documented for btrfs-progs; not captured from a live scrub.
    s = btrfs.scrub_summary("UUID:  x\nScrub started:    Sat Nov 14 11:30:26 2026\nStatus:           running\nDuration:         0:00:05\n")
    assert s["running"] and s["status"] == "running"


def test_balance_states():
    assert btrfs.balance_summary(fixture("balance_none.txt"))["state"] == "none"
    running = "Balance on '/mnt/pool' is running\n3 out of about 10 chunks balanced (4 considered),  70% left\n"
    assert btrfs.balance_summary(running)["state"] == "running"
    assert btrfs.balance_summary("Balance on '/mnt/pool' is paused\n")["state"] == "paused"
    assert btrfs.balance_summary("Balance on '/mnt/pool' is running, cancel requested\n")["state"] == "cancelling"


# ---- snapper ----

def test_snapper_configs():
    assert snapper.parse_csv(fixture("snapper_configs.csv")) == [
        {"config": "data", "subvolume": "/mnt/pool/@data"}]


def test_snapper_snapshots_skip_current_and_handle_csv_quoting():
    snaps = snapper.parse_snapshots(fixture("snapper_snapshots.csv"))
    assert [s["number"] for s in snaps] == [1, 2, 3]          # 0 is "current"
    assert snaps[0]["description"] == "first, with comma"
    assert snaps[1]["description"] == 'quote "x" test' and snaps[1]["cleanup"] == "timeline"


def test_snapper_pre_post_types():
    text = ("number,default,active,date,user,cleanup,description,userdata,pre-number,post-number,post-date\n"
            "5,no,no,2026-01-01 10:00:00,root,number,pre,,,6,2026-01-01 10:01:00\n"
            "6,no,no,2026-01-01 10:01:00,root,number,post,,5,,\n")
    a, b = snapper.parse_snapshots(text)
    assert (a["type"], b["type"], b["pre_number"]) == ("pre", "post", 5)


def test_snapper_getconfig():
    rows = snapper.parse_csv(fixture("snapper_getconfig.csv"))
    cfg = {r["key"]: r["value"] for r in rows}
    assert cfg["SUBVOLUME"] == "/mnt/pool/@data" and cfg["TIMELINE_LIMIT_HOURLY"] == "5"


def test_snapper_status():
    changes, truncated = snapper.parse_status(fixture("snapper_status.txt"))
    assert changes == [{"flags": "+.....", "path": "/mnt/pool/@data/new.txt"}] and not truncated
