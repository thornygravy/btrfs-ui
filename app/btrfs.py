"""Thin wrappers around btrfs-progs. Parsers are pure functions of the text
output so they can be tested without a filesystem."""
import re

BTRFS = "btrfs"


# ---- filesystem show --raw -------------------------------------------------

_LABEL = re.compile(r"^Label:\s+(?:none|'(?P<label>.*)')\s+uuid:\s+(?P<uuid>[0-9a-fA-F-]+)")
_TOTAL = re.compile(r"Total devices\s+(\d+)\s+FS bytes used\s+(\d+)")
_DEVID = re.compile(
    r"^\s*devid\s+(\d+)\s+size\s+(\d+)\s+used\s+(\d+)\s+path\s+(.+?)(\s+MISSING)?\s*$")


def parse_show(text):
    out = {"label": "", "uuid": None, "bytes_used": None, "devices": []}
    for line in text.splitlines():
        m = _LABEL.match(line)
        if m:
            out["label"] = m.group("label") or ""
            out["uuid"] = m.group("uuid")
            continue
        m = _TOTAL.search(line)
        if m:
            out["bytes_used"] = int(m.group(2))
            continue
        m = _DEVID.match(line)
        if m:
            out["devices"].append({
                "devid": int(m.group(1)), "size": int(m.group(2)),
                "used": int(m.group(3)), "path": m.group(4),
                "missing": bool(m.group(5)),
            })
    return out


# ---- filesystem df -b ------------------------------------------------------

_DF = re.compile(r"^([\w+]+), (\w+)(?: \(multiple profiles\))?: total=(\d+), used=(\d+)")


def parse_df(text):
    rows = []
    for line in text.splitlines():
        m = _DF.match(line.strip())
        if m:
            rows.append({"type": m.group(1), "profile": m.group(2),
                         "total": int(m.group(3)), "used": int(m.group(4))})
    return rows


# ---- filesystem usage -b (the "Overall" block) ------------------------------

_USAGE = re.compile(r"^\s+([A-Za-z][A-Za-z ()/,]*?):\s+(\S+)(?:\s+\(min:\s*(\d+)\))?")


def parse_usage(text):
    out = {}
    in_overall = False
    for line in text.splitlines():
        if line.startswith("Overall:"):
            in_overall = True
            continue
        if in_overall and not line.strip():
            break
        if not in_overall:
            continue
        m = _USAGE.match(line)
        if not m:
            continue
        key = re.sub(r"[^a-z0-9]+", "_", m.group(1).lower()).strip("_")
        raw = m.group(2)
        if re.fullmatch(r"\d+", raw):
            out[key] = int(raw)
        elif re.fullmatch(r"\d+\.\d+", raw):
            out[key] = float(raw)
        else:
            out[key] = raw
        if m.group(3) is not None:
            out[key + "_min"] = int(m.group(3))
    return out


_DEV_LINE = re.compile(r"^\s+(\S+)\s+(\d+)\s*$")


def parse_usage_devices(text):
    """Per-device allocated / unallocated bytes from `filesystem usage -b`.

    `filesystem show` needs the block device nodes under /dev to report sizes;
    inside a container without /dev it prints size 0 and MISSING for every
    device. The usage output comes straight from the kernel, so it is the
    reliable source for device sizes.
    """
    out = {}
    section = None
    for line in text.splitlines():
        if line.startswith("Overall:") or not line.strip():
            section = None
            continue
        if not line.startswith(" "):
            section = "unallocated" if line.startswith("Unallocated:") else "allocated"
            continue
        m = _DEV_LINE.match(line)
        if m and section:
            d = out.setdefault(m.group(1), {"allocated": 0, "unallocated": 0})
            d[section] += int(m.group(2))
    return out


# ---- device stats ----------------------------------------------------------

_STAT = re.compile(r"^\[(.+)\]\.(\w+)\s+(\d+)\s*$")


def parse_device_stats(text):
    out = {}
    for line in text.splitlines():
        m = _STAT.match(line.strip())
        if m:
            out.setdefault(m.group(1), {})[m.group(2)] = int(m.group(3))
    return out


# ---- subvolume list --------------------------------------------------------

_INT_KEYS = {"ID": "id", "gen": "gen", "cgen": "cgen", "parent": "parent"}
_STR_KEYS = {"parent_uuid", "received_uuid", "uuid"}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^\d{2}:\d{2}:\d{2}$")


def parse_subvol_list(text):
    """Parse `btrfs subvolume list` output in any combination of -p -c -g -u -q -R -s."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("ID "):
            continue
        head, sep, path = line.partition(" path ")
        if not sep:
            continue
        sv = {"path": path}
        t = head.split()
        i = 0
        while i < len(t):
            k = t[i]
            if k in _INT_KEYS and i + 1 < len(t) and t[i + 1].isdigit():
                sv[_INT_KEYS[k]] = int(t[i + 1]); i += 2
            elif k == "top" and i + 2 < len(t) and t[i + 1] == "level":
                sv["top_level"] = int(t[i + 2]); i += 3
            elif k == "otime" and i + 1 < len(t):
                if i + 2 < len(t) and _DATE.match(t[i + 1]) and _TIME.match(t[i + 2]):
                    sv["otime"] = t[i + 1] + " " + t[i + 2]; i += 3
                else:
                    sv["otime"] = t[i + 1]; i += 2
            elif k in _STR_KEYS and i + 1 < len(t):
                sv[k] = None if t[i + 1] == "-" else t[i + 1]; i += 2
            else:
                i += 1
        if "id" in sv:
            out.append(sv)
    return out


def parse_default_id(text):
    m = re.match(r"\s*ID\s+(\d+)", text)
    return int(m.group(1)) if m else None


# ---- qgroups ---------------------------------------------------------------

_QGROUP = re.compile(r"^\s*0/(\d+)\s+(\d+)\s+(\d+)\b")


def parse_qgroups(text):
    out = {}
    for line in text.splitlines():
        m = _QGROUP.match(line)
        if m:
            out[int(m.group(1))] = {"referenced": int(m.group(2)),
                                    "exclusive": int(m.group(3))}
    return out


# ---- scrub / balance status ------------------------------------------------

_PAIR = re.compile(r"^\s*([A-Za-z][A-Za-z ]*?):\s*(.*?)\s*$")


def parse_pairs(text):
    """Key/value lines such as 'Status:   finished' -> [[key, value], ...]."""
    out = []
    for line in text.splitlines():
        m = _PAIR.match(line)
        if m:
            out.append([m.group(1), m.group(2)])
    return out


def scrub_summary(text):
    pairs = parse_pairs(text)
    status = next((v for k, v in pairs if k == "Status"), None)
    return {
        "text": text.strip(),
        "pairs": pairs,
        "running": status == "running",
        "status": status or ("never run" if "no stats available" in text else "unknown"),
    }


def balance_summary(text):
    low = text.lower()
    if "no balance found" in low:
        state = "none"
    elif "cancel requested" in low:
        state = "cancelling"
    elif "is paused" in low:
        state = "paused"
    elif "is running" in low:
        state = "running"
    else:
        state = "unknown"
    return {"state": state, "text": text.strip()}
