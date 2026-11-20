"""API behaviour with the command runner faked out."""
import pytest

from app import proc, snapper as snapper_mod
from app.config import Config
from app.server import create_app
from conftest import fixture

UUID = "81182bc3-6b88-4f7b-83b7-33f99a1f706e"
H = {"X-Requested-With": "btrfs-ui"}


class Runner:
    """Stands in for proc.run / proc.run_detached and records every command."""

    def __init__(self):
        self.calls = []
        self.detached = []
        self.snapper_csv = {}

    def run(self, args, timeout=60, check=True):
        self.calls.append(list(args))
        a = args
        if a[0] == "btrfs":
            if a[1:] == ["--version"]:
                return 0, "btrfs-progs v6.6.3\n", ""
            sub = tuple(a[1:3])
            out = {
                ("filesystem", "show"): fixture("show_raw.txt"),
                ("filesystem", "df"): fixture("df_b.txt"),
                ("filesystem", "usage"): fixture("usage_b.txt"),
                ("device", "stats"): fixture("device_stats.txt"),
                ("scrub", "status"): fixture("scrub_status.txt"),
                ("balance", "status"): fixture("balance_none.txt"),
                ("qgroup", "show"): fixture("qgroup_show.txt"),
            }.get(sub)
            if sub == ("subvolume", "list"):
                flags = a[3:-1]
                out = fixture({"-s": "subvol_list_s.txt", "-r": "subvol_list_r.txt"}.get(
                    flags[0] if len(flags) == 1 else "", "subvol_list_full.txt"))
            if sub == ("subvolume", "get-default"):
                out = "ID 5 (FS_TREE)\n"
            return 0, out or "", ""
        if a[0] == "snapper":
            if "list-configs" in a:
                return 0, fixture("snapper_configs.csv"), ""
            if "get-config" in a:
                return 0, fixture("snapper_getconfig.csv"), ""
            if "create" in a:
                return 0, "4\n", ""
            if "list" in a:
                return 0, fixture("snapper_snapshots.csv"), ""
            if "status" in a:
                return 0, fixture("snapper_status.txt"), ""
            return 0, "", ""
        return 0, "", ""

    def run_detached(self, args, wait=30):
        self.detached.append(list(args))
        return 0, ""


@pytest.fixture
def runner(monkeypatch):
    r = Runner()
    monkeypatch.setattr(proc, "run", r.run)
    monkeypatch.setattr(proc, "run_detached", r.run_detached)
    monkeypatch.setattr(snapper_mod.Snapper, "available", property(lambda self: True))
    monkeypatch.setattr("app.server.time.sleep", lambda s: None)
    return r


@pytest.fixture
def pool(tmp_path):
    root = tmp_path / "pool"
    (root / "snaps").mkdir(parents=True)
    info = tmp_path / "mountinfo"
    info.write_text(f"50 27 0:41 / {root} rw - btrfs /dev/loop0 rw,subvolid=5,subvol=/\n")
    return root, str(info)


def make_client(pool, **env):
    base = {"BTRFS_UI_AUTH": "off", "BTRFS_UI_MOUNTINFO": pool[1]}
    base.update(env)
    app = create_app(Config(base))
    app.testing = True
    return app.test_client()


# ---- authentication -----------------------------------------------------------

def test_refuses_to_start_without_password():
    with pytest.raises(SystemExit):
        Config({}).validate()
    Config({"BTRFS_UI_AUTH": "off"}).validate()
    Config({"BTRFS_UI_PASSWORD": "x"}).validate()


def test_refuses_placeholder_password():
    with pytest.raises(SystemExit):
        Config({"BTRFS_UI_PASSWORD": "change-me"}).validate()


def test_password_from_file(tmp_path):
    f = tmp_path / "secret"; f.write_text("hunter2\n")
    assert Config({"BTRFS_UI_PASSWORD_FILE": str(f)}).password == "hunter2"


def test_login_flow(runner, pool):
    c = make_client(pool, BTRFS_UI_AUTH="on", BTRFS_UI_PASSWORD="s3cret")
    assert c.get("/api/filesystems").status_code == 401
    assert c.get("/api/session").get_json()["authenticated"] is False
    assert c.post("/api/login", json={"password": "nope"}, headers=H).status_code == 401
    assert c.get("/api/filesystems").status_code == 401
    assert c.post("/api/login", json={"password": "s3cret"}, headers=H).status_code == 200
    assert c.get("/api/filesystems").status_code == 200
    c.post("/api/logout", headers=H)
    assert c.get("/api/filesystems").status_code == 401


def test_login_is_rate_limited(runner, pool):
    c = make_client(pool, BTRFS_UI_AUTH="on", BTRFS_UI_PASSWORD="s3cret")
    for _ in range(5):
        assert c.post("/api/login", json={"password": "x"}, headers=H).status_code == 401
    assert c.post("/api/login", json={"password": "s3cret"}, headers=H).status_code == 429


def test_static_page_is_open_but_api_is_not(runner, pool):
    c = make_client(pool, BTRFS_UI_AUTH="on", BTRFS_UI_PASSWORD="s3cret")
    assert c.get("/").status_code == 200
    assert c.get("/api/jobs").status_code == 401
    assert c.get("/api/snapper/configs").status_code == 401


def test_changes_need_the_csrf_header(runner, pool):
    c = make_client(pool)
    r = c.post(f"/api/fs/{UUID}/trim", json={})
    assert r.status_code == 400 and "X-Requested-With" in r.get_json()["error"]


def test_session_cookie_is_strict_and_httponly(runner, pool):
    c = make_client(pool, BTRFS_UI_AUTH="on", BTRFS_UI_PASSWORD="s3cret")
    r = c.post("/api/login", json={"password": "s3cret"}, headers=H)
    cookie = r.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie


def test_readonly_mode(runner, pool):
    c = make_client(pool, BTRFS_UI_READONLY="1")
    assert c.get(f"/api/fs/{UUID}/overview").status_code == 200
    assert c.get("/api/session").get_json()["readonly"] is True
    for url, body in [(f"/api/fs/{UUID}/subvolumes/create", {"path": "x"}),
                      (f"/api/fs/{UUID}/scrub", {"action": "start"}),
                      ("/api/snapper/data/snapshots", {"description": "x"})]:
        assert c.post(url, json=body, headers=H).status_code == 403
    assert not runner.detached
    assert not any(c_[1:3] == ["subvolume", "create"] for c_ in runner.calls)


# ---- read endpoints --------------------------------------------------------------

def test_overview(runner, pool):
    d = make_client(pool).get(f"/api/fs/{UUID}/overview").get_json()
    assert d["label"] == "testpool"
    assert d["usage"]["device_size"] == 1258291200
    assert [x["path"] for x in d["devices"]] == ["/dev/loop0", "/dev/loop1"]
    assert d["devices"][0]["size"] == 629145600 and not d["devices"][0]["missing"]
    assert d["scrub"]["status"] == "finished" and d["balance"]["state"] == "none"


def test_overview_survives_missing_dev_nodes(runner, pool, monkeypatch):
    real = runner.run

    def run(args, timeout=60, check=True):
        if args[1:3] == ["filesystem", "show"]:
            return 0, fixture("show_nodev.txt"), ""
        return real(args, timeout, check)
    monkeypatch.setattr(proc, "run", run)
    d = make_client(pool).get(f"/api/fs/{UUID}/overview").get_json()
    assert all(not x["missing"] and x["size"] == 629145600 for x in d["devices"])


def test_subvolume_list(runner, pool):
    root = pool[0]
    d = make_client(pool).get(f"/api/fs/{UUID}/subvolumes").get_json()
    by = {s["path"]: s for s in d["subvolumes"]}
    assert d["quotas"] and d["top_level_mounted"]
    assert by["snaps/data-1"]["snapshot"] and by["snaps/data-1"]["readonly"]
    assert by["snaps/data-1"]["created"] == "2026-11-14 11:26:19"
    assert not by["@data"]["snapshot"] and not by["@data"]["readonly"]
    assert by["@data"]["abs"] == f"{root}/@data"
    assert by["@data"]["referenced"] == 20987904


def test_unknown_filesystem_is_404(runner, pool):
    assert make_client(pool).get("/api/fs/nope/overview").status_code == 404


# ---- changes ---------------------------------------------------------------------

def test_snapshot_command(runner, pool):
    root = pool[0]
    c = make_client(pool)
    r = c.post(f"/api/fs/{UUID}/subvolumes/snapshot", headers=H,
               json={"id": 256, "dest": "snaps/data-new", "readonly": True})
    assert r.status_code == 200, r.get_json()
    assert ["btrfs", "subvolume", "snapshot", "-r", f"{root}/@data", f"{root}/snaps/data-new"] in runner.calls
    r = c.post(f"/api/fs/{UUID}/subvolumes/snapshot", headers=H,
               json={"id": 256, "dest": "snaps/data-new2", "readonly": False})
    assert ["btrfs", "subvolume", "snapshot", f"{root}/@data", f"{root}/snaps/data-new2"] in runner.calls


def test_snapshot_rejects_bad_destinations(runner, pool):
    c = make_client(pool)
    root = pool[0]
    (root / "snaps" / "taken").mkdir()
    for dest in ["../escape", "snaps/taken", "", "missing-parent/x"]:
        r = c.post(f"/api/fs/{UUID}/subvolumes/snapshot", headers=H, json={"id": 256, "dest": dest})
        assert r.status_code in (400, 404), dest
    assert r.get_json()["error"]
    assert not any(x[1:3] == ["subvolume", "snapshot"] for x in runner.calls)


def test_delete_guards(runner, pool, monkeypatch):
    c = make_client(pool)
    url = f"/api/fs/{UUID}/subvolumes/delete"
    assert c.post(url, headers=H, json={"id": 5}).status_code == 400
    assert c.post(url, headers=H, json={"id": 9999}).status_code == 404
    assert c.post(url, headers=H, json={"id": "abc"}).status_code == 400
    assert c.post(url, headers=H, json={"id": 258}).status_code == 200
    assert ["btrfs", "subvolume", "delete", "-i", "258", str(pool[0])] in runner.calls


def test_delete_refuses_mounted_subvolume(runner, pool, tmp_path):
    # also mount subvolume 256 (@data) somewhere
    data = tmp_path / "data"; data.mkdir()
    with open(pool[1], "a") as fh:
        fh.write(f"51 27 0:41 /@data {data} rw - btrfs /dev/loop0 rw,subvolid=256,subvol=/@data\n")
    c = make_client(pool)
    r = c.post(f"/api/fs/{UUID}/subvolumes/delete", headers=H, json={"id": 256})
    assert r.status_code == 400 and "mounted" in r.get_json()["error"]


def test_scrub_and_balance_commands(runner, pool):
    c = make_client(pool)
    mp = str(pool[0])
    c.post(f"/api/fs/{UUID}/scrub", headers=H, json={"action": "start", "readonly": True})
    c.post(f"/api/fs/{UUID}/scrub", headers=H, json={"action": "start"})
    c.post(f"/api/fs/{UUID}/balance", headers=H, json={"action": "start", "data_usage": 40, "metadata_usage": "10"})
    assert ["btrfs", "scrub", "start", "-r", mp] in runner.detached
    assert ["btrfs", "scrub", "start", mp] in runner.detached
    assert ["btrfs", "balance", "start", "--background", "-dusage=40", "-musage=10", mp] in runner.detached

    r = c.post(f"/api/fs/{UUID}/balance", headers=H, json={"action": "start"})
    assert r.status_code == 400                       # refuses an unfiltered balance by accident
    c.post(f"/api/fs/{UUID}/balance", headers=H, json={"action": "start", "full": True})
    assert ["btrfs", "balance", "start", "--background", "--full-balance", mp] in runner.detached
    for bad in [{"data_usage": 101}, {"data_usage": -1}, {"data_usage": "x; reboot"}]:
        r = c.post(f"/api/fs/{UUID}/balance", headers=H, json={"action": "start", **bad})
        assert r.status_code == 400, bad
    assert c.post(f"/api/fs/{UUID}/scrub", headers=H, json={"action": "explode"}).status_code == 400


def test_defrag_validation(runner, pool, monkeypatch):
    started = []

    class FakeJobs(proc.JobManager):
        def start(self, title, key, args):
            started.append(args)
            return proc.Job(title, key, args)
    monkeypatch.setattr(proc, "JobManager", FakeJobs)
    c = make_client(pool)
    root = pool[0]
    url = f"/api/fs/{UUID}/defrag"
    assert c.post(url, headers=H, json={"path": "snaps", "compression": "zstd", "target_size": "32M"}).status_code == 200
    assert started[-1] == ["btrfs", "filesystem", "defragment", "-r", "-czstd", "-t", "32M", f"{root}/snaps"]
    for bad in [{"path": "snaps", "compression": "rm -rf"}, {"path": "snaps", "target_size": "32M; id"},
                {"path": "../../etc"}, {"path": ""}]:
        assert c.post(url, headers=H, json=bad).status_code in (400, 404), bad


# ---- snapper ---------------------------------------------------------------------

def test_snapper_snapshots_and_unknown_config(runner, pool):
    c = make_client(pool)
    assert [s["number"] for s in c.get("/api/snapper/data/snapshots").get_json()] == [1, 2, 3]
    assert c.get("/api/snapper/nope/snapshots").status_code == 400


def test_snapper_description_is_a_single_argument(runner, pool):
    c = make_client(pool)
    nasty = '"; rm -rf / #  $(reboot) `id`'
    r = c.post("/api/snapper/data/snapshots", headers=H, json={"description": nasty, "cleanup": "number"})
    assert r.get_json() == {"number": "4"}
    call = next(x for x in runner.calls if x[0] == "snapper" and "create" in x)
    assert nasty in call and call.count(nasty) == 1
    assert call[:4] == ["snapper", "--no-dbus", "-c", "data"]
    assert c.post("/api/snapper/data/snapshots", headers=H, json={"cleanup": "bogus"}).status_code == 400


def test_snapper_delete_validation(runner, pool):
    c = make_client(pool)
    url = "/api/snapper/data/delete"
    assert c.post(url, headers=H, json={"numbers": [0]}).status_code == 400
    assert c.post(url, headers=H, json={"numbers": []}).status_code == 400
    assert c.post(url, headers=H, json={"numbers": ["1; id"]}).status_code == 400
    assert c.post(url, headers=H, json={"numbers": [3, 1, 3]}).status_code == 200
    assert ["snapper", "--no-dbus", "-c", "data", "delete", "1", "3"] in runner.calls


def test_snapper_settings_validation(runner, pool):
    c = make_client(pool)
    url = "/api/snapper/data/settings"
    assert c.post(url, headers=H, json={"values": {"SUBVOLUME": "/"}}).status_code == 400
    assert c.post(url, headers=H, json={"values": {"NUMBER_LIMIT": "5 SUBVOLUME=/"}}).status_code == 400
    assert c.post(url, headers=H, json={"values": {"TIMELINE_CREATE": "maybe"}}).status_code == 400
    assert c.post(url, headers=H, json={"values": {"NUMBER_LIMIT": "5-10", "TIMELINE_CREATE": "no"}}).status_code == 200
    assert not any("SUBVOLUME=/" in x for call in runner.calls for x in call)
    got = c.get(url).get_json()
    assert got["subvolume"] == "/mnt/pool/@data" and "NUMBER_LIMIT" in got["values"]


def test_snapper_status_args(runner, pool):
    c = make_client(pool)
    assert c.get("/api/snapper/data/status?from=1&to=3").status_code == 200
    assert ["snapper", "--no-dbus", "-c", "data", "status", "1..3"] in runner.calls
    assert c.get("/api/snapper/data/status?from=1;id&to=3").status_code == 400


def test_snapper_root_option(runner, pool):
    c = make_client(pool, BTRFS_UI_SNAPPER_ROOT="/host")
    c.get("/api/snapper/configs")
    assert any(x[:4] == ["snapper", "--no-dbus", "--root", "/host"] for x in runner.calls)


def test_session_reports_tool_version_only_when_signed_in(runner, pool):
    c = make_client(pool, BTRFS_UI_AUTH="on", BTRFS_UI_PASSWORD="s3cret")
    assert c.get("/api/session").get_json()["btrfsProgs"] is None
    c.post("/api/login", json={"password": "s3cret"}, headers=H)
    d = c.get("/api/session").get_json()
    assert d["btrfsProgs"] == "6.6.3" and d["version"]


# ---- the Snapper tab is only offered when /etc/snapper is mounted ---------------------

def _with_mount(pool, tmp_path, extra_line):
    info = tmp_path / "mountinfo-snapper"
    info.write_text(open(pool[1]).read() + extra_line)
    return str(info)


def test_snapper_tab_is_hidden_without_the_mount(runner, pool):
    assert make_client(pool).get("/api/session").get_json()["snapper"] is False


def test_snapper_tab_is_shown_when_etc_snapper_is_mounted(runner, pool, tmp_path):
    line = "61 27 8:1 /etc/snapper /etc/snapper ro - ext4 /dev/sda1 rw\n"
    c = make_client(pool, BTRFS_UI_MOUNTINFO=_with_mount(pool, tmp_path, line))
    assert c.get("/api/session").get_json()["snapper"] is True


def test_snapper_tab_ignores_look_alike_mounts(runner, pool, tmp_path):
    line = ("61 27 8:1 /x /etc/snapper-old ro - ext4 /dev/sda1 rw\n"
            "62 27 8:1 /y /srv/etc/snapper ro - ext4 /dev/sda1 rw\n")
    c = make_client(pool, BTRFS_UI_MOUNTINFO=_with_mount(pool, tmp_path, line))
    assert c.get("/api/session").get_json()["snapper"] is False


def test_snapper_tab_is_shown_with_a_snapper_root(runner, pool):
    assert make_client(pool, BTRFS_UI_SNAPPER_ROOT="/host").get("/api/session").get_json()["snapper"] is True


def test_snapper_tab_is_hidden_when_snapper_is_not_installed(runner, pool, tmp_path, monkeypatch):
    monkeypatch.setattr(snapper_mod.Snapper, "available", property(lambda self: False))
    line = "61 27 8:1 /etc/snapper /etc/snapper ro - ext4 /dev/sda1 rw\n"
    c = make_client(pool, BTRFS_UI_MOUNTINFO=_with_mount(pool, tmp_path, line))
    assert c.get("/api/session").get_json()["snapper"] is False
