"""Discover filesystems from the mount table and resolve user supplied paths
safely (they must end up inside a mounted btrfs filesystem)."""
import os
import posixpath
import threading
import time

from . import btrfs, mounts as mounts_mod, proc


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def clean_rel(path):
    """Validate a top-level-relative path like '@data/photos'."""
    p = (path or "").strip().strip("/")
    if not p or "\x00" in p:
        raise ApiError("A path is required")
    if any(part in ("", ".", "..") for part in p.split("/")):
        raise ApiError("Path may not contain '.', '..' or empty parts")
    return p


class Fs:
    def __init__(self, uuid, label, mounts):
        self.id = uuid
        self.uuid = uuid
        self.label = label
        self.mounts = mounts

    @property
    def primary(self):
        """Best mount to run filesystem-wide commands on: the top level if it
        is mounted, otherwise the shortest mount path."""
        return sorted(self.mounts, key=lambda m: (m.root_rel != "", len(m.mount_point)))[0]

    def reach(self, rel):
        """Absolute in-container path for a top-level-relative path."""
        best = None
        for m in self.mounts:
            p = m.reach(rel)
            if p is not None and (best is None or len(m.root_rel) > best[0]):
                best = (len(m.root_rel), p)
        return best[1] if best else None

    def _inside(self, path):
        real = os.path.realpath(path)
        for m in self.mounts:
            base = os.path.realpath(m.mount_point)
            if real == base or real.startswith(base + os.sep):
                return True
        return False

    def resolve(self, user_path, must_exist=True):
        """Turn what the user typed into an absolute path inside this filesystem.

        Accepts either an absolute path under one of this filesystem's mount
        points, or a path relative to the top level ('@data/photos')."""
        s = (user_path or "").strip()
        if s.startswith("/") and any(
                s == m.mount_point or s.startswith(m.mount_point.rstrip("/") + "/")
                for m in self.mounts):
            if ".." in s.split("/") or "\x00" in s:
                raise ApiError("Path may not contain '..'")
            path = posixpath.normpath(s)
        else:
            rel = clean_rel(s)
            path = self.reach(rel)
            if path is None:
                raise ApiError(
                    f"'{rel}' is not reachable from any directory mounted in this "
                    "container. Mount the filesystem's top level (subvolid=5) to see everything.")
        check = path if must_exist else os.path.dirname(path)
        if not os.path.exists(check):
            raise ApiError("No such path: " + (path if must_exist else check), 404)
        if not self._inside(check):
            raise ApiError("Path resolves outside the filesystem")
        return path


class Registry:
    def __init__(self, cfg, ttl=5.0):
        self.cfg = cfg
        self._ttl = ttl
        self._lock = threading.Lock()
        self._cache = (0.0, [])

    def all(self):
        with self._lock:
            at, cached = self._cache
            if time.time() - at < self._ttl:
                return cached
            groups = {}
            for m in mounts_mod.read_btrfs_mounts(self.cfg.mountinfo):
                rc, out, _ = proc.run(
                    [btrfs.BTRFS, "filesystem", "show", "--raw", m.mount_point],
                    check=False, timeout=20)
                info = btrfs.parse_show(out) if rc == 0 else {"uuid": None}
                if not info["uuid"]:
                    continue
                g = groups.setdefault(info["uuid"], Fs(info["uuid"], info["label"], []))
                g.mounts.append(m)
            result = sorted(groups.values(), key=lambda f: (f.label, f.uuid))
            self._cache = (time.time(), result)
            return result

    def get(self, fsid):
        for f in self.all():
            if f.id == fsid:
                return f
        raise ApiError("Unknown filesystem", 404)
