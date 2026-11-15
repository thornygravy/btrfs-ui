"""Find the btrfs filesystems that are visible inside this container."""
import os
import re
from dataclasses import dataclass


@dataclass
class Mount:
    mount_point: str
    root: str          # path of this mount's root inside the filesystem
    source: str
    subvol: str
    subvolid: int | None
    options: str

    @property
    def root_rel(self):
        """Top-level-relative path this mount starts at ('' for the top level)."""
        root = self.root
        if root == "/" and self.subvol not in ("", "/"):
            root = self.subvol
        return root.strip("/")

    def reach(self, rel):
        """Map a top-level-relative path to a path inside this container,
        or None if the mount does not cover it."""
        base = self.root_rel
        if base == "":
            sub = rel
        elif rel == base:
            sub = ""
        elif rel.startswith(base + "/"):
            sub = rel[len(base) + 1:]
        else:
            return None
        return self.mount_point if not sub else os.path.join(self.mount_point, sub)


def _unescape(s):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def read_btrfs_mounts(path="/proc/self/mountinfo"):
    mounts = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            left, sep, right = line.rstrip("\n").partition(" - ")
            if not sep:
                continue
            lf = left.split(" ")
            rf = right.split(" ", 2)
            if len(lf) < 6 or len(rf) < 3 or rf[0] != "btrfs":
                continue
            opts = {}
            for item in rf[2].split(","):
                k, _, v = item.partition("=")
                opts[k] = v
            mount_point = _unescape(lf[4])
            if not os.path.isdir(mount_point):
                continue  # docker bind-mounts single files such as /etc/hosts
            sid = opts.get("subvolid")
            mounts[mount_point] = Mount(
                mount_point=mount_point,
                root=_unescape(lf[3]),
                source=_unescape(rf[1]),
                subvol=_unescape(opts.get("subvol", "")),
                subvolid=int(sid) if sid and sid.isdigit() else None,
                options=rf[2],
            )
    return list(mounts.values())
