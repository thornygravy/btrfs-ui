import os

import pytest

from app import mounts
from app.fs import ApiError, Fs, clean_rel


def mountinfo(tmp_path, lines):
    p = tmp_path / "mountinfo"
    p.write_text("\n".join(lines) + "\n")
    return str(p)


def test_read_btrfs_mounts(tmp_path):
    top = tmp_path / "pool"; top.mkdir()
    home = tmp_path / "my stuff"; home.mkdir()
    ext4 = tmp_path / "other"; ext4.mkdir()
    hostname = tmp_path / "hostname"; hostname.write_text("x")
    esc = str(home).replace(" ", "\\040")
    path = mountinfo(tmp_path, [
        f"50 27 0:41 / {top} rw,relatime - btrfs /dev/sda rw,subvolid=5,subvol=/",
        f"51 27 0:41 /@home {esc} rw - btrfs /dev/sda rw,subvolid=257,subvol=/@home",
        f"52 27 0:30 / {ext4} rw - ext4 /dev/sdb rw",
        f"53 27 0:41 /@/docker/hostname {hostname} rw - btrfs /dev/sda rw,subvolid=5,subvol=/",   # a file
        "garbage line",
    ])
    found = {m.mount_point: m for m in mounts.read_btrfs_mounts(path)}
    assert set(found) == {str(top), str(home)}
    assert found[str(top)].subvolid == 5 and found[str(top)].root_rel == ""
    assert found[str(home)].root_rel == "@home" and found[str(home)].subvol == "/@home"


def test_reach_prefers_most_specific_mount(tmp_path):
    top = mounts.Mount("/mnt/pool", "/", "/dev/sda", "/", 5, "")
    data = mounts.Mount("/srv/data", "/@data", "/dev/sda", "/@data", 256, "")
    fs = Fs("u", "l", [top, data])
    assert fs.reach("@data/photos") == "/srv/data/photos"
    assert fs.reach("@data") == "/srv/data"
    assert fs.reach("@home/x") == "/mnt/pool/@home/x"
    only_data = Fs("u", "l", [data])
    assert only_data.reach("@home/x") is None
    assert only_data.reach("@data-old/x") is None      # prefix must be a whole path component
    assert fs.primary is top


def test_clean_rel():
    assert clean_rel("/@data/photos/") == "@data/photos"
    for bad in ["", "   ", "../x", "a/../b", "a//b", "./a", "a\x00b"]:
        with pytest.raises(ApiError):
            clean_rel(bad)


def test_resolve_rejects_escapes(tmp_path):
    root = tmp_path / "pool"; root.mkdir()
    (root / "sub").mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    os.symlink(outside, root / "link")
    fs = Fs("u", "l", [mounts.Mount(str(root), "/", "/dev/x", "/", 5, "")])

    assert fs.resolve("sub") == str(root / "sub")
    assert fs.resolve(str(root / "sub")) == str(root / "sub")
    assert fs.resolve("sub/new", must_exist=False) == str(root / "sub" / "new")
    with pytest.raises(ApiError):
        fs.resolve("../outside")
    with pytest.raises(ApiError):
        fs.resolve(str(root) + "/../outside")
    with pytest.raises(ApiError):
        fs.resolve("link")                       # symlink leading out of the filesystem
    with pytest.raises(ApiError):
        fs.resolve("link/new", must_exist=False)
    with pytest.raises(ApiError):
        fs.resolve("missing")
