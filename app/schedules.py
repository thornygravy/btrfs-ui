"""Opt-in scheduled snapshots and scrubs.

Rules live in one JSON file in the data directory, a volume the user adds to
the compose file. Nothing is scheduled unless that directory exists, and
nothing runs in read-only mode.

Two kinds of rule:

* ``snapshot``: take a read-only snapshot of a subvolume into a directory and
  keep only the newest ``keep`` snapshots made by this rule. Retention only
  ever deletes a snapshot when ALL of these hold: it lives directly in the
  rule's destination directory, its name is ``<prefix>-YYYY-MM-DD_HHMM``, it
  is read-only, and it is a snapshot of the rule's source subvolume.
* ``scrub``: start a scrub, unless one is already running.

Times are the container's local time (set ``TZ`` to change it).
"""
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from datetime import datetime, timedelta

from . import btrfs, clock, proc
from .fs import ApiError, clean_rel

log = logging.getLogger("btrfs-ui")
B = btrfs.BTRFS

TYPES = ("snapshot", "scrub")
FREQUENCIES = ("hourly", "daily", "weekly", "monthly")
MAX_RULES = 50
HISTORY_LIMIT = 200
PREFIX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")
CLOCK = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
STAMP = "%Y-%m-%d_%H%M"


# ---- when things are due ---------------------------------------------------
# All of this works on naive local datetimes, so it is easy to test.

def latest_slot(rule, now):
    """The most recent scheduled moment at or before `now`."""
    hh, mm = (int(x) for x in rule["time"].split(":"))
    freq = rule["frequency"]
    if freq == "hourly":
        slot = now.replace(minute=mm, second=0, microsecond=0)
        if slot > now:
            slot -= timedelta(hours=1)
        return slot
    slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if freq == "daily":
        if slot > now:
            slot -= timedelta(days=1)
    elif freq == "weekly":
        slot -= timedelta(days=(slot.weekday() - rule["weekday"]) % 7)
        if slot > now:
            slot -= timedelta(days=7)
    else:  # monthly; the day is limited to 1-28 so every month has it
        slot = slot.replace(day=rule["monthday"])
        if slot > now:
            slot = (slot.replace(day=1) - timedelta(days=1)).replace(day=rule["monthday"])
    return slot


def next_slot(rule, now):
    """The first scheduled moment after `now`."""
    cur = latest_slot(rule, now)
    freq = rule["frequency"]
    if freq == "hourly":
        return cur + timedelta(hours=1)
    if freq == "daily":
        return cur + timedelta(days=1)
    if freq == "weekly":
        return cur + timedelta(days=7)
    return (cur.replace(day=28) + timedelta(days=4)).replace(day=rule["monthday"])


def _epoch(dt):
    return time.mktime(dt.timetuple())


def _fmt(ts):
    """Container-local time, the same clock snapper and `btrfs scrub status` print."""
    return clock.wall(ts)


def is_due(rule, now):
    """True when a scheduled moment has passed that this rule has not handled yet."""
    return _epoch(latest_slot(rule, now)) > rule["handled"]


# ---- retention -------------------------------------------------------------

def select_expired(rule, subvols, source_uuid):
    """Snapshots this rule made that are beyond its `keep` limit (oldest first).

    Deliberately strict: anything that does not match every condition is left
    alone, even if its name looks right."""
    if not source_uuid:
        return []
    name = re.compile(re.escape(rule["prefix"]) + r"-\d{4}-\d{2}-\d{2}_\d{4}")
    mine = []
    for s in subvols:
        parent, _, base = s["path"].rpartition("/")
        if parent != rule["dest"] or not name.fullmatch(base):
            continue
        if not s.get("readonly") or s.get("parent_uuid") != source_uuid:
            continue
        if s.get("default") or s.get("mounted"):
            continue
        mine.append(s)
    mine.sort(key=lambda s: s["path"])  # the name holds the time, so this is oldest first
    return mine[:-rule["keep"]] if len(mine) > rule["keep"] else []


# ---- validation ------------------------------------------------------------

def _int(value, name, lo, hi):
    if isinstance(value, bool) or value in (None, ""):
        raise ApiError(f"'{name}' is required")
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ApiError(f"'{name}' must be a number")
    if not lo <= n <= hi:
        raise ApiError(f"'{name}' must be between {lo} and {hi}")
    return n


def _when(data):
    freq = data.get("frequency")
    if freq not in FREQUENCIES:
        raise ApiError("Frequency must be hourly, daily, weekly or monthly")
    out = {"frequency": freq}
    if freq == "hourly":
        out["time"] = "00:%02d" % _int(data.get("minute"), "minute", 0, 59)
    else:
        t = str(data.get("time") or "")
        if not CLOCK.fullmatch(t):
            raise ApiError("Time must look like 03:00")
        out["time"] = t
    if freq == "weekly":
        out["weekday"] = _int(data.get("weekday"), "weekday", 0, 6)
    if freq == "monthly":
        out["monthday"] = _int(data.get("monthday"), "day of the month", 1, 28)
    return out


def _default_prefix(source):
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", source.rsplit("/", 1)[-1]).strip("-._")
    return base[:40] or "snapshot"


def _stored_rule_ok(r):
    """Cheap sanity check of a rule read back from disk (it may be hand edited)."""
    try:
        ok = (isinstance(r, dict) and r["type"] in TYPES and isinstance(r["id"], str)
              and isinstance(r["fs"], str) and isinstance(r["enabled"], bool)
              and r["frequency"] in FREQUENCIES and CLOCK.fullmatch(r["time"])
              and isinstance(r["created"], (int, float)) and isinstance(r["handled"], (int, float))
              and (r["frequency"] != "weekly" or 0 <= r["weekday"] <= 6)
              and (r["frequency"] != "monthly" or 1 <= r["monthday"] <= 28))
        if ok and r["type"] == "snapshot":
            ok = (isinstance(r["source"], str) and isinstance(r["dest"], str)
                  and PREFIX.fullmatch(r["prefix"]) and isinstance(r["keep"], int)
                  and not isinstance(r["keep"], bool) and r["keep"] >= 1)
        return bool(ok)
    except (KeyError, TypeError):
        return False


# ---- storage ---------------------------------------------------------------

class Store:
    """The rules and a short run history, in one JSON file."""

    def __init__(self, data_dir):
        self.dir = data_dir
        self.path = os.path.join(data_dir, "schedules.json")
        self.lock = threading.RLock()
        self.rules = []
        self.history = []
        self.reason = None  # why schedules are unavailable, or None when they work
        self.missing = False  # True when nothing is mounted at the data directory
        if not os.path.isdir(data_dir):
            self.missing = True
            self.reason = f"{data_dir} does not exist in this container."
        elif not os.access(data_dir, os.W_OK):
            self.reason = f"{data_dir} is not writable."
        else:
            self._load()

    @property
    def enabled(self):
        return self.reason is None

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            self.reason = f"Could not read {self.path} ({e}). Fix or remove it, then restart."
            return
        rules = data.get("rules") if isinstance(data, dict) else None
        history = data.get("history", []) if isinstance(data, dict) else None
        if not isinstance(rules, list) or not isinstance(history, list) or not all(
                _stored_rule_ok(r) for r in rules):
            # Never "repair" it by overwriting: the file may be the only copy of the rules.
            self.reason = f"{self.path} has an unexpected format. Fix or remove it, then restart."
            return
        self.rules = rules
        self.history = [h for h in history if isinstance(h, dict)][-HISTORY_LIMIT:]

    def save(self):
        with self.lock:
            payload = {"version": 1, "rules": self.rules, "history": self.history[-HISTORY_LIMIT:]}
            fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".schedules-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise


# ---- the scheduler ---------------------------------------------------------

class Scheduler:
    def __init__(self, cfg, registry, collect_subvols):
        self.cfg = cfg
        self.registry = registry
        self._collect = collect_subvols
        self.store = Store(cfg.data_dir)
        self._run_lock = threading.Lock()  # one rule runs at a time
        self._thread = None

    # -- background loop --

    @property
    def active(self):
        return self.store.enabled and not self.cfg.readonly

    def start(self, interval=30):
        if not self.active or self._thread:
            return

        def loop():
            while True:
                try:
                    self.tick()
                except Exception:
                    log.exception("scheduler tick failed")
                time.sleep(interval)

        self._thread = threading.Thread(target=loop, name="scheduler", daemon=True)
        self._thread.start()
        log.info("Scheduler running with %d rule(s)", len(self.store.rules))

    def tick(self, now_ts=None):
        """Run every rule that is due. Returns [(rule id, status)] for what ran."""
        if not self.active:
            return []
        now_ts = time.time() if now_ts is None else now_ts
        now = datetime.fromtimestamp(now_ts)
        ran = []
        for rule in list(self.store.rules):
            if rule["enabled"] and is_due(rule, now):
                entry = self._execute(rule, "schedule", now_ts, _epoch(latest_slot(rule, now)))
                ran.append((rule["id"], entry["status"]))
        return ran

    # -- running one rule --

    def _execute(self, rule, trigger, now_ts, slot_ts):
        with self._run_lock:
            now = datetime.fromtimestamp(now_ts)
            try:
                try:
                    f = self.registry.get(rule["fs"])
                except ApiError:
                    f = None
                if f is None:
                    status, message = "skipped", "The filesystem is not mounted in this container"
                elif rule["type"] == "snapshot":
                    status, message = self._run_snapshot(rule, f, now)
                else:
                    status, message = self._run_scrub(f)
            except (ApiError, proc.CmdError) as e:
                status, message = "error", e.message
            except Exception:
                log.exception("schedule %s failed", rule["id"])
                status, message = "error", "Unexpected error (see container logs)"
            entry = {"time": now_ts, "rule": rule["id"], "fs": rule["fs"], "what": self.label(rule),
                     "trigger": trigger, "status": status, "message": message}
            with self.store.lock:
                rule["last_run"], rule["last_status"], rule["last_message"] = now_ts, status, message
                if slot_ts is not None:
                    rule["handled"] = slot_ts
                self.store.history.append(entry)
                self.store.save()
            log.info("schedule %s (%s): %s: %s", rule["id"], self.label(rule), status, message)
            return entry

    def _run_snapshot(self, rule, f, now):
        subvols, _ = self._collect(f)
        src = next((s for s in subvols if s["path"] == rule["source"]), None)
        if src is None:
            raise ApiError(f"Source subvolume '{rule['source']}' no longer exists")
        if not src["abs"]:
            raise ApiError("The source subvolume is not reachable from this container's mounts")
        dest_abs = f.resolve(rule["dest"])
        if not os.path.isdir(dest_abs):
            raise ApiError(f"Destination '{rule['dest']}' is not a directory")
        name = f"{rule['prefix']}-{now.strftime(STAMP)}"
        target = os.path.join(dest_abs, name)
        if os.path.exists(target):
            return "skipped", f"{name} already exists"
        proc.run([B, "subvolume", "snapshot", "-r", src["abs"], target], timeout=300)

        subvols, _ = self._collect(f)  # fresh listing, so retention counts the new snapshot
        removed, failures = 0, []
        for old in select_expired(rule, subvols, src.get("uuid")):
            try:
                proc.run([B, "subvolume", "delete", "-i", str(old["id"]), f.primary.mount_point],
                         timeout=300)
                removed += 1
            except proc.CmdError as e:
                failures.append(f"{old['path']}: {e.message}")
        message = f"Created {name}"
        if removed:
            message += f", removed {removed} old"
        if failures:
            return "error", message + "; could not remove " + "; ".join(failures)
        return "ok", message

    def _run_scrub(self, f):
        mp = f.primary.mount_point
        rc, out, err = proc.run([B, "scrub", "status", mp], check=False)
        if btrfs.scrub_summary(out or err)["running"]:
            return "skipped", "A scrub is already running"
        proc.run_detached([B, "scrub", "start", mp])
        return "ok", "Scrub started"

    @staticmethod
    def label(rule):
        return f"Snapshot of {rule['source']}" if rule["type"] == "snapshot" else "Scrub"

    # -- what the API uses --

    def _require_enabled(self):
        if not self.store.enabled:
            raise ApiError("Schedules are not available: " + self.store.reason)

    def _find(self, rule_id):
        for r in self.store.rules:
            if r["id"] == rule_id:
                return r
        raise ApiError("No such schedule", 404)

    def validate(self, data):
        typ = data.get("type")
        if typ not in TYPES:
            raise ApiError("Type must be 'snapshot' or 'scrub'")
        f = self.registry.get(str(data.get("fs") or ""))
        rule = {"type": typ, "fs": f.id, "enabled": True, **_when(data)}
        if typ == "snapshot":
            source, dest = clean_rel(data.get("source")), clean_rel(data.get("dest"))
            subvols, _ = self._collect(f)
            src = next((s for s in subvols if s["path"] == source), None)
            if src is None:
                raise ApiError(f"'{source}' is not a subvolume of this filesystem")
            if not src["abs"]:
                raise ApiError("That subvolume is not reachable from this container's mounts")
            if not os.path.isdir(f.resolve(dest)):
                raise ApiError("The destination must be an existing directory")
            prefix = str(data.get("prefix") or "").strip() or _default_prefix(source)
            if not PREFIX.fullmatch(prefix):
                raise ApiError("The name prefix may use letters, digits, '.', '_' and '-' (up to 40)")
            rule.update(source=source, dest=dest, prefix=prefix,
                        keep=_int(data.get("keep"), "keep", 1, 1000))
        return rule

    def create(self, data):
        self._require_enabled()
        rule = self.validate(data)
        with self.store.lock:
            if len(self.store.rules) >= MAX_RULES:
                raise ApiError(f"At most {MAX_RULES} schedules")
            if rule["type"] == "snapshot" and any(
                    r["type"] == "snapshot" and (r["fs"], r["source"], r["dest"], r["prefix"]) ==
                    (rule["fs"], rule["source"], rule["dest"], rule["prefix"])
                    for r in self.store.rules):
                raise ApiError("A schedule with the same source, destination and name prefix "
                               "already exists. Use a different prefix.")
            now = time.time()
            rule.update(id=secrets.token_hex(4), created=now, handled=now,
                        last_run=None, last_status=None, last_message=None)
            self.store.rules.append(rule)
            self.store.save()
        return rule

    def set_enabled(self, rule_id, enabled):
        self._require_enabled()
        with self.store.lock:
            rule = self._find(rule_id)
            if enabled and not rule["enabled"]:
                rule["handled"] = time.time()  # do not catch up on what was missed while off
            rule["enabled"] = bool(enabled)
            self.store.save()
        return rule

    def delete(self, rule_id):
        self._require_enabled()
        with self.store.lock:
            self.store.rules.remove(self._find(rule_id))
            self.store.save()

    def run_now(self, rule_id):
        self._require_enabled()
        rule = self._find(rule_id)
        return self._execute(rule, "manual", time.time(), None)

    def overview(self):
        now_ts = time.time()
        now = datetime.fromtimestamp(now_ts)
        with self.store.lock:
            rules = []
            for r in self.store.rules:
                item = dict(r)
                item["next"] = _epoch(next_slot(r, now)) if r["enabled"] else None
                item["next_text"] = _fmt(item["next"])
                item["last_text"] = _fmt(r.get("last_run"))
                rules.append(item)
            history = [dict(h, time_text=_fmt(h.get("time")))
                       for h in reversed(self.store.history[-60:])]
        return {
            "enabled": self.store.enabled, "reason": self.store.reason,
            "missing": self.store.missing,
            "dataDir": self.cfg.data_dir, "readonly": self.cfg.readonly,
            "timezone": clock.zone(),
            "rules": rules, "history": history,
        }
