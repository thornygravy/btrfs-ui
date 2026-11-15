"""Runs the real API against a real btrfs filesystem on a loopback file.

Needs root, losetup, mkfs.btrfs and permission to mount; skipped otherwise."""
import os
import shutil
import subprocess
import time

import pytest

from app.config import Config
from app.server import create_app

H = {"X-Requested-With": "btrfs-ui"}
LABEL = f"btrfsui-test-{os.getpid()}"


def sh(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(scope="module")
def mountpoint(tmp_path_factory):
    if os.geteuid() != 0 or not all(shutil.which(t) for t in ("mkfs.btrfs", "losetup", "mount", "btrfs")):
        pytest.skip("needs root and btrfs tools")
    d = tmp_path_factory.mktemp("loop")
    img, mnt = d / "fs.img", d / "mnt"
    mnt.mkdir()
    sh("truncate", "-s", "300M", str(img))
    sh("mkfs.btrfs", "-q", "-f", "-L", LABEL, str(img))
    try:
        loop = sh("losetup", "-f", "--show", str(img))
    except subprocess.CalledProcessError:
        pytest.skip("no loop device available")
    try:
        try:
            sh("mount", "-t", "btrfs", loop, str(mnt))
        except subprocess.CalledProcessError:
            pytest.skip("cannot mount btrfs here")
        try:
            yield mnt
        finally:
            sh("umount", str(mnt))
    finally:
        sh("losetup", "-d", loop)


@pytest.fixture(scope="module")
def api(mountpoint):
    app = create_app(Config({"BTRFS_UI_AUTH": "off"}))
    c = app.test_client()
    fs = next(f for f in c.get("/api/filesystems").get_json() if f["label"] == LABEL)
    return c, fs["id"], mountpoint


def wait_for_job(c, job_id, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        j = c.get(f"/api/jobs/{job_id}").get_json()
        if j["state"] != "running":
            return j
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def test_overview(api):
    c, fs, _ = api
    d = c.get(f"/api/fs/{fs}/overview").get_json()
    assert d["label"] == LABEL and d["devices"][0]["size"] > 200 * 2**20
    assert not d["devices"][0]["missing"]


def test_subvolume_lifecycle(api):
    c, fs, mnt = api
    url = f"/api/fs/{fs}/subvolumes"
    assert c.post(url + "/create", json={"path": "data"}, headers=H).status_code == 200
    (mnt / "data" / "file.txt").write_text("hello")
    r = c.post(url + "/create", json={"path": "data"}, headers=H)
    assert r.status_code == 400                                   # already exists

    items = c.get(url).get_json()["subvolumes"]
    data = next(s for s in items if s["path"] == "data")
    r = c.post(url + "/snapshot", json={"id": data["id"], "dest": "data-snap", "readonly": True}, headers=H)
    assert r.status_code == 200, r.get_json()
    assert (mnt / "data-snap" / "file.txt").read_text() == "hello"

    items = c.get(url).get_json()["subvolumes"]
    snap = next(s for s in items if s["path"] == "data-snap")
    assert snap["snapshot"] and snap["readonly"] and snap["created"]

    assert c.post(url + "/readonly", json={"id": snap["id"], "readonly": False}, headers=H).status_code == 200
    items = c.get(url).get_json()["subvolumes"]
    assert not next(s for s in items if s["path"] == "data-snap")["readonly"]

    assert c.post(url + "/delete", json={"id": snap["id"]}, headers=H).status_code == 200
    assert c.post(url + "/delete", json={"id": data["id"]}, headers=H).status_code == 200
    assert not [s for s in c.get(url).get_json()["subvolumes"] if s["path"] in ("data", "data-snap")]


def test_path_escape_is_refused(api):
    c, fs, mnt = api
    outside = mnt.parent / "outside"
    outside.mkdir()
    os.symlink(outside, mnt / "link")
    r = c.post(f"/api/fs/{fs}/subvolumes/create", json={"path": "link/evil"}, headers=H)
    assert r.status_code == 400 and not (outside / "evil").exists()


def test_scrub(api):
    c, fs, _ = api
    assert c.post(f"/api/fs/{fs}/scrub", json={"action": "start"}, headers=H).status_code == 200
    # Right after the launcher exits, status can still read "never run" until the
    # background scrub registers itself, so wait for the final state.
    for _ in range(100):
        s = c.get(f"/api/fs/{fs}/scrub").get_json()
        if s["status"] == "finished":
            break
        time.sleep(0.2)
    assert s["status"] == "finished"


def test_balance_and_jobs(api):
    c, fs, mnt = api
    assert c.post(f"/api/fs/{fs}/balance", json={"action": "start", "data_usage": 90}, headers=H).status_code == 200
    for _ in range(50):
        if c.get(f"/api/fs/{fs}/balance").get_json()["state"] == "none":
            break
        time.sleep(0.2)
    (mnt / "plain").write_text("x" * 1000)
    job = c.post(f"/api/fs/{fs}/defrag", json={"path": "plain", "recursive": False}, headers=H).get_json()["job"]
    assert wait_for_job(c, job["id"])["state"] == "done"
    r = c.post(f"/api/fs/{fs}/defrag", json={"path": "nope"}, headers=H)
    assert r.status_code == 404
