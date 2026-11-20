"""Snapper integration. Talks to snapper directly (--no-dbus), so no daemon or
D-Bus is needed in the container."""
import csv
import io
import re
import shutil

from . import mounts, proc

CONFIG_DIR = "/etc/snapper"

NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
SNAP_COLUMNS = ("number,default,active,date,user,cleanup,description,"
                "userdata,pre-number,post-number,post-date")

# Settings that can be changed from the UI, and what a valid value looks like.
_YESNO = re.compile(r"^(yes|no)$")
_LIMIT = re.compile(r"^\d+(-\d+)?$")
_AGE = re.compile(r"^\d+$")
SETTINGS = {
    "TIMELINE_CREATE": _YESNO, "TIMELINE_CLEANUP": _YESNO,
    "TIMELINE_MIN_AGE": _AGE,
    "TIMELINE_LIMIT_HOURLY": _LIMIT, "TIMELINE_LIMIT_DAILY": _LIMIT,
    "TIMELINE_LIMIT_WEEKLY": _LIMIT, "TIMELINE_LIMIT_MONTHLY": _LIMIT,
    "TIMELINE_LIMIT_YEARLY": _LIMIT,
    "NUMBER_CLEANUP": _YESNO, "NUMBER_MIN_AGE": _AGE,
    "NUMBER_LIMIT": _LIMIT, "NUMBER_LIMIT_IMPORTANT": _LIMIT,
    "EMPTY_PRE_POST_CLEANUP": _YESNO, "EMPTY_PRE_POST_MIN_AGE": _AGE,
}
CLEANUP_ALGORITHMS = ("number", "timeline", "empty-pre-post")


def parse_csv(text):
    """CSV with a header row -> list of dicts keyed by header (dashes -> underscores)."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    header = [h.replace("-", "_") for h in rows[0]]
    return [dict(zip(header, r)) for r in rows[1:] if r]


def parse_snapshots(text):
    out = []
    for r in parse_csv(text):
        if not r.get("number", "").isdigit() or r["number"] == "0":
            continue  # snapshot 0 is the live filesystem
        kind = "post" if r.get("pre_number") else "pre" if r.get("post_number") else "single"
        out.append({
            "number": int(r["number"]), "type": kind,
            "pre_number": int(r["pre_number"]) if r.get("pre_number") else None,
            "date": r.get("date", ""), "user": r.get("user", ""),
            "cleanup": r.get("cleanup", ""), "description": r.get("description", ""),
            "userdata": r.get("userdata", ""),
            "default": r.get("default") == "yes", "active": r.get("active") == "yes",
        })
    return out


def parse_status(text, limit=2000):
    out = []
    for line in text.splitlines():
        m = re.match(r"^(\S{6})\s+(.*)$", line)
        if m:
            out.append({"flags": m.group(1), "path": m.group(2)})
    return out[:limit], len(out) > limit


class Snapper:
    def __init__(self, cfg):
        self.root = cfg.snapper_root
        self.mountinfo = cfg.mountinfo

    @property
    def available(self):
        return shutil.which("snapper") is not None

    @property
    def shown(self):
        """Whether the UI offers the Snapper tab: snapper is installed and the host's
        /etc/snapper was mounted into the container (or a --root was configured)."""
        return self.available and (
            self.root is not None or mounts.is_mounted(CONFIG_DIR, self.mountinfo))

    def _base(self, config=None, csv_out=False):
        a = ["snapper", "--no-dbus"]
        if self.root:
            a += ["--root", self.root]
        if csv_out:
            a.append("--csvout")
        if config:
            a += ["-c", config]
        return a

    def configs(self):
        if not self.available:
            return []
        _, out, _ = proc.run(self._base(csv_out=True) + ["list-configs", "--columns", "config,subvolume"])
        return parse_csv(out)

    def check(self, name):
        if not NAME_RE.match(name or "") or name not in [c["config"] for c in self.configs()]:
            raise proc.CmdError(f"Unknown snapper config '{name}'")

    def snapshots(self, name):
        _, out, _ = proc.run(
            self._base(name, True) + ["list", "--disable-used-space", "--columns", SNAP_COLUMNS],
            timeout=120)
        return parse_snapshots(out)

    def create(self, name, description, cleanup=None):
        args = self._base(name) + ["create", "--print-number", "--description", description]
        if cleanup:
            args += ["--cleanup-algorithm", cleanup]
        _, out, _ = proc.run(args, timeout=300)
        return out.strip()

    def delete(self, name, numbers):
        proc.run(self._base(name) + ["delete"] + [str(n) for n in numbers], timeout=600)

    def status(self, name, a, b):
        _, out, _ = proc.run(self._base(name) + ["status", f"{a}..{b}"], timeout=300)
        return parse_status(out)

    def get_config(self, name):
        _, out, _ = proc.run(self._base(name, True) + ["get-config"])
        return {r["key"]: r.get("value", "") for r in parse_csv(out)}

    def set_config(self, name, values):
        pairs = []
        for k, v in values.items():
            rx = SETTINGS.get(k)
            if rx is None:
                raise proc.CmdError(f"Setting '{k}' cannot be changed here")
            v = str(v).strip()
            if not rx.match(v):
                raise proc.CmdError(f"Invalid value for {k}: '{v}'")
            pairs.append(f"{k}={v}")
        if pairs:
            proc.run(self._base(name) + ["set-config"] + pairs)

    def cleanup_args(self, name, algorithm):
        return self._base(name) + ["cleanup", algorithm]
