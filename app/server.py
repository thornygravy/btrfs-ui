"""HTTP API and static UI."""
import hmac
import logging
import os
import re
import time
from datetime import timedelta
from functools import wraps

from flask import Flask, jsonify, request, send_from_directory, session

from . import btrfs, clock, proc, snapper as snapper_mod
from .config import Config
from .fs import ApiError, Registry
from .schedules import Scheduler

log = logging.getLogger("btrfs-ui")
B = btrfs.BTRFS
VERSION = "1.0"


def create_app(cfg=None):
    cfg = cfg or Config()
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.secret_key = cfg.secret
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=cfg.cookie_secure,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    )
    registry = Registry(cfg)
    jobs = proc.JobManager()
    snap = snapper_mod.Snapper(cfg)
    failures = {}  # client address -> recent failed login times

    # ---- plumbing ----------------------------------------------------------

    @app.errorhandler(ApiError)
    def _api_error(e):
        return jsonify(error=e.message), e.status

    @app.errorhandler(proc.CmdError)
    def _cmd_error(e):
        return jsonify(error=e.message), 400

    @app.errorhandler(404)
    def _not_found(e):
        return jsonify(error="Not found"), 404

    @app.errorhandler(Exception)
    def _unexpected(e):
        log.exception("unhandled error")
        return jsonify(error="Internal error (see container logs)"), 500

    OPEN = ("/", "/api/session", "/api/login")

    @app.before_request
    def _guard():
        path = request.path
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            # Browsers cannot add this header on a cross-site request without
            # a CORS preflight, which we never approve.
            if request.headers.get("X-Requested-With") != "btrfs-ui":
                raise ApiError("Missing X-Requested-With header", 400)
        if path.startswith("/api/") and path not in OPEN:
            if cfg.auth_enabled and not session.get("auth"):
                raise ApiError("Sign in required", 401)

    @app.after_request
    def _headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "frame-ancestors 'none'")
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    def mutating(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if cfg.readonly:
                raise ApiError("This instance is read-only (BTRFS_UI_READONLY is set)", 403)
            log.info("action %s %s from %s", request.method, request.path, request.remote_addr)
            return fn(*a, **kw)
        return wrapper

    def body():
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    def int_field(data, name, lo=None, hi=None, required=True):
        v = data.get(name)
        if v in (None, ""):
            if required:
                raise ApiError(f"'{name}' is required")
            return None
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise ApiError(f"'{name}' must be a number")
        if (lo is not None and n < lo) or (hi is not None and n > hi):
            raise ApiError(f"'{name}' is out of range")
        return n

    def text_of(args, timeout=60):
        return proc.run(args, timeout=timeout)[1]

    def try_text(args, warnings=None, label=None, timeout=60):
        rc, out, err = proc.run(args, check=False, timeout=timeout)
        if rc != 0:
            if warnings is not None and label:
                warnings.append(f"{label}: {proc.clean_error(err or out)}")
            return None
        return out

    # ---- session -----------------------------------------------------------

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/favicon.ico")
    def favicon():
        return "", 204

    progs = []  # cached btrfs-progs version, looked up once

    def progs_version():
        if not progs:
            try:
                _, out, _ = proc.run([B, "--version"], timeout=5, check=False)
                m = re.search(r"v?(\d+(?:\.\d+)+)", out)
                progs.append(m.group(1) if m else "")
            except Exception:  # footer info only, never worth failing a request
                progs.append("")
        return progs[0]

    @app.get("/api/session")
    def get_session():
        authenticated = (not cfg.auth_enabled) or bool(session.get("auth"))
        return jsonify(
            authRequired=cfg.auth_enabled,
            authenticated=authenticated,
            readonly=cfg.readonly,
            snapper=snap.shown,
            version=VERSION,
            # server details are only for signed-in users
            btrfsProgs=progs_version() if authenticated else None,
            timezone=clock.zone() if authenticated else None,
            schedules=app.scheduler.store.enabled if authenticated else None,
        )

    @app.post("/api/login")
    def login():
        who = request.remote_addr or "?"
        now = time.time()
        recent = [t for t in failures.get(who, []) if now - t < 300]
        if len(recent) >= 5:
            raise ApiError("Too many failed attempts, try again in a few minutes", 429)
        supplied = str(body().get("password", ""))
        if not cfg.auth_enabled or (
                cfg.password and hmac.compare_digest(supplied.encode(), cfg.password.encode())):
            session.clear()
            session.permanent = True
            session["auth"] = True
            failures.pop(who, None)
            return jsonify(ok=True)
        failures[who] = recent + [now]
        time.sleep(1)
        raise ApiError("Wrong password", 401)

    @app.post("/api/logout")
    def logout():
        session.clear()
        return jsonify(ok=True)

    # ---- filesystems -------------------------------------------------------

    def fs_summary(f):
        return {
            "id": f.id, "uuid": f.uuid, "label": f.label,
            "primary": f.primary.mount_point,
            "mounts": [{"path": m.mount_point, "subvol": m.subvol or "/",
                        "subvolid": m.subvolid, "source": m.source}
                       for m in sorted(f.mounts, key=lambda m: m.mount_point)],
        }

    @app.get("/api/filesystems")
    def list_filesystems():
        return jsonify([fs_summary(f) for f in registry.all()])

    @app.get("/api/fs/<fsid>/overview")
    def overview(fsid):
        f = registry.get(fsid)
        mp = f.primary.mount_point
        warnings = []
        show = btrfs.parse_show(text_of([B, "filesystem", "show", "--raw", mp]))
        df = btrfs.parse_df(try_text([B, "filesystem", "df", "-b", mp], warnings, "df") or "")
        usage_text = try_text([B, "filesystem", "usage", "-b", mp], warnings, "usage") or ""
        stats = btrfs.parse_device_stats(
            try_text([B, "device", "stats", mp], warnings, "device stats") or "")
        per_dev = btrfs.parse_usage_devices(usage_text)
        for d in show["devices"]:
            d["errors"] = stats.get(d["path"], {})
            d["error_total"] = sum(d["errors"].values())
            known = per_dev.get(d["path"])
            if known:  # prefer kernel numbers; `show` reports 0/MISSING without /dev nodes
                d["size"] = known["allocated"] + known["unallocated"]
                d["used"] = known["allocated"]
                d["missing"] = False
        scrub = btrfs.scrub_summary(
            try_text([B, "scrub", "status", mp], warnings, "scrub status") or "")
        balance = btrfs.balance_summary(
            try_text([B, "balance", "status", mp], warnings, "balance status") or "")
        return jsonify(
            **fs_summary(f), devices=show["devices"], bytes_used=show["bytes_used"],
            allocation=df, usage=btrfs.parse_usage(usage_text), usage_text=usage_text,
            scrub=scrub, balance=balance, warnings=warnings)

    # ---- subvolumes --------------------------------------------------------

    def collect_subvols(f):
        mp = f.primary.mount_point
        listing = btrfs.parse_subvol_list(
            text_of([B, "subvolume", "list", "-p", "-c", "-g", "-u", "-q", "-R", mp]))
        snaps = {s["id"]: s for s in btrfs.parse_subvol_list(
            try_text([B, "subvolume", "list", "-s", mp]) or "")}
        ro = {s["id"] for s in btrfs.parse_subvol_list(
            try_text([B, "subvolume", "list", "-r", mp]) or "")}
        default_id = btrfs.parse_default_id(try_text([B, "subvolume", "get-default", mp]) or "")
        qgroups = btrfs.parse_qgroups(try_text([B, "qgroup", "show", "--raw", mp]) or "")
        mounted = {m.subvolid for m in f.mounts if m.subvolid is not None}
        for sv in listing:
            sid = sv["id"]
            sv["abs"] = f.reach(sv["path"])
            sv["readonly"] = sid in ro
            sv["snapshot"] = sid in snaps or bool(sv.get("parent_uuid"))
            sv["created"] = snaps.get(sid, {}).get("otime")
            sv["default"] = sid == default_id
            sv["mounted"] = sid in mounted
            sv.update(qgroups.get(sid, {}))
        listing.sort(key=lambda s: s["path"])
        return listing, bool(qgroups)

    def find_subvol(f, sid):
        for sv in collect_subvols(f)[0]:
            if sv["id"] == sid:
                return sv
        raise ApiError("Subvolume not found", 404)

    @app.get("/api/fs/<fsid>/subvolumes")
    def subvolumes(fsid):
        f = registry.get(fsid)
        items, has_quota = collect_subvols(f)
        return jsonify(subvolumes=items, quotas=has_quota,
                       top_level_mounted=any(m.root_rel == "" for m in f.mounts))

    @app.post("/api/fs/<fsid>/subvolumes/create")
    @mutating
    def subvolume_create(fsid):
        f = registry.get(fsid)
        dest = f.resolve(body().get("path"), must_exist=False)
        if os.path.exists(dest):
            raise ApiError("That path already exists")
        proc.run([B, "subvolume", "create", dest])
        return jsonify(ok=True)

    @app.post("/api/fs/<fsid>/subvolumes/snapshot")
    @mutating
    def subvolume_snapshot(fsid):
        f = registry.get(fsid)
        data = body()
        src = find_subvol(f, int_field(data, "id", lo=1))
        if not src["abs"]:
            raise ApiError("This subvolume is not reachable from anything mounted in the container")
        dest = f.resolve(data.get("dest"), must_exist=False)
        if os.path.exists(dest):
            raise ApiError("The destination already exists")
        args = [B, "subvolume", "snapshot"]
        if data.get("readonly"):
            args.append("-r")
        proc.run(args + [src["abs"], dest], timeout=300)
        return jsonify(ok=True)

    @app.post("/api/fs/<fsid>/subvolumes/delete")
    @mutating
    def subvolume_delete(fsid):
        f = registry.get(fsid)
        sid = int_field(body(), "id", lo=1)
        if sid == 5:
            raise ApiError("The top-level subvolume cannot be deleted")
        sv = find_subvol(f, sid)
        if sv["default"]:
            raise ApiError("This is the default subvolume; change the default first")
        if sv["mounted"]:
            raise ApiError("This subvolume is mounted in the container; unmount it first")
        proc.run([B, "subvolume", "delete", "-i", str(sid), f.primary.mount_point], timeout=300)
        return jsonify(ok=True)

    @app.post("/api/fs/<fsid>/subvolumes/readonly")
    @mutating
    def subvolume_readonly(fsid):
        f = registry.get(fsid)
        data = body()
        sv = find_subvol(f, int_field(data, "id", lo=1))
        if not sv["abs"]:
            raise ApiError("This subvolume is not reachable from anything mounted in the container")
        value = "true" if data.get("readonly") else "false"
        proc.run([B, "property", "set", "-ts", sv["abs"], "ro", value])
        return jsonify(ok=True)

    # ---- scrub / balance / defrag / trim -----------------------------------

    @app.get("/api/fs/<fsid>/scrub")
    def scrub_status(fsid):
        mp = registry.get(fsid).primary.mount_point
        rc, out, err = proc.run([B, "scrub", "status", mp], check=False)
        return jsonify(btrfs.scrub_summary(out or err))

    @app.post("/api/fs/<fsid>/scrub")
    @mutating
    def scrub_action(fsid):
        mp = registry.get(fsid).primary.mount_point
        data = body()
        action = data.get("action")
        if action == "start":
            args = [B, "scrub", "start"] + (["-r"] if data.get("readonly") else []) + [mp]
            proc.run_detached(args)
        elif action == "resume":
            proc.run_detached([B, "scrub", "resume", mp])
        elif action == "cancel":
            proc.run([B, "scrub", "cancel", mp])
        else:
            raise ApiError("Unknown action")
        return jsonify(ok=True)

    @app.get("/api/fs/<fsid>/balance")
    def balance_status(fsid):
        mp = registry.get(fsid).primary.mount_point
        rc, out, err = proc.run([B, "balance", "status", mp], check=False)
        return jsonify(btrfs.balance_summary(out or err))

    @app.post("/api/fs/<fsid>/balance")
    @mutating
    def balance_action(fsid):
        mp = registry.get(fsid).primary.mount_point
        data = body()
        action = data.get("action")
        if action == "cancel":
            proc.run([B, "balance", "cancel", mp])
        elif action == "start":
            args = [B, "balance", "start", "--background"]
            dusage = int_field(data, "data_usage", 0, 100, required=False)
            musage = int_field(data, "metadata_usage", 0, 100, required=False)
            if dusage is not None:
                args.append(f"-dusage={dusage}")
            if musage is not None:
                args.append(f"-musage={musage}")
            if dusage is None and musage is None:
                if not data.get("full"):
                    raise ApiError("Enter a usage percentage for data and/or metadata chunks")
                args.append("--full-balance")
            proc.run_detached(args + [mp])
        else:
            raise ApiError("Unknown action")
        return jsonify(ok=True)

    @app.post("/api/fs/<fsid>/defrag")
    @mutating
    def defrag(fsid):
        f = registry.get(fsid)
        data = body()
        path = f.resolve(data.get("path"))
        args = [B, "filesystem", "defragment"]
        if data.get("recursive", True):
            args.append("-r")
        comp = data.get("compression") or ""
        if comp:
            if comp not in ("zstd", "zlib", "lzo"):
                raise ApiError("Unknown compression")
            args.append("-c" + comp)
        size = (data.get("target_size") or "").strip()
        if size:
            if not re.fullmatch(r"\d+[KMG]?", size):
                raise ApiError("Target extent size looks like 32M")
            args += ["-t", size]
        job = jobs.start(f"Defragment {path}", f"defrag:{f.id}", args + [path])
        return jsonify(job=job.to_dict())

    @app.post("/api/fs/<fsid>/trim")
    @mutating
    def trim(fsid):
        f = registry.get(fsid)
        mp = f.primary.mount_point
        job = jobs.start(f"Trim {mp}", f"trim:{f.id}", ["fstrim", "-v", mp])
        return jsonify(job=job.to_dict())

    # ---- jobs --------------------------------------------------------------

    @app.get("/api/jobs")
    def list_jobs():
        return jsonify([j.to_dict() for j in jobs.list()])

    @app.get("/api/jobs/<job_id>")
    def get_job(job_id):
        j = jobs.get(job_id)
        if not j:
            raise ApiError("No such job", 404)
        return jsonify(j.to_dict(full=True))

    @app.post("/api/jobs/<job_id>/cancel")
    @mutating
    def cancel_job(job_id):
        if not jobs.cancel(job_id):
            raise ApiError("Job is not running")
        return jsonify(ok=True)

    # ---- snapper -----------------------------------------------------------

    @app.get("/api/snapper/configs")
    def snapper_configs():
        if not snap.available:
            return jsonify(available=False, configs=[])
        return jsonify(available=True, configs=snap.configs())

    @app.get("/api/snapper/<name>/snapshots")
    def snapper_snapshots(name):
        snap.check(name)
        return jsonify(snap.snapshots(name))

    @app.post("/api/snapper/<name>/snapshots")
    @mutating
    def snapper_create(name):
        snap.check(name)
        data = body()
        desc = str(data.get("description") or "").strip()[:200]
        cleanup = data.get("cleanup") or None
        if cleanup and cleanup not in snapper_mod.CLEANUP_ALGORITHMS:
            raise ApiError("Unknown cleanup algorithm")
        return jsonify(number=snap.create(name, desc, cleanup))

    @app.post("/api/snapper/<name>/delete")
    @mutating
    def snapper_delete(name):
        snap.check(name)
        nums = body().get("numbers")
        if not isinstance(nums, list) or not nums or len(nums) > 100:
            raise ApiError("Give a list of snapshot numbers")
        try:
            nums = sorted({int(n) for n in nums})
        except (TypeError, ValueError):
            raise ApiError("Snapshot numbers must be integers")
        if any(n < 1 for n in nums):
            raise ApiError("Snapshot 0 is the live filesystem and cannot be deleted")
        snap.delete(name, nums)
        return jsonify(ok=True)

    @app.get("/api/snapper/<name>/status")
    def snapper_status(name):
        snap.check(name)
        a = int_field(request.args, "from", lo=0)
        b = int_field(request.args, "to", lo=0)
        changes, truncated = snap.status(name, a, b)
        return jsonify(changes=changes, truncated=truncated)

    @app.get("/api/snapper/<name>/settings")
    def snapper_settings(name):
        snap.check(name)
        cfg_all = snap.get_config(name)
        return jsonify(
            subvolume=cfg_all.get("SUBVOLUME", ""),
            values={k: cfg_all.get(k, "") for k in snapper_mod.SETTINGS})

    @app.post("/api/snapper/<name>/settings")
    @mutating
    def snapper_set_settings(name):
        snap.check(name)
        values = body().get("values")
        if not isinstance(values, dict):
            raise ApiError("Missing values")
        snap.set_config(name, values)
        return jsonify(ok=True)

    @app.post("/api/snapper/<name>/cleanup")
    @mutating
    def snapper_cleanup(name):
        snap.check(name)
        algo = body().get("algorithm")
        if algo not in snapper_mod.CLEANUP_ALGORITHMS:
            raise ApiError("Unknown cleanup algorithm")
        job = jobs.start(f"Snapper cleanup ({algo}) for {name}", f"snapper:{name}",
                         snap.cleanup_args(name, algo))
        return jsonify(job=job.to_dict())

    # ---- schedules ---------------------------------------------------------

    app.scheduler = scheduler = Scheduler(cfg, registry, collect_subvols)

    @app.get("/api/schedules")
    def schedules_list():
        return jsonify(scheduler.overview())

    @app.post("/api/schedules")
    @mutating
    def schedules_create():
        return jsonify(rule=scheduler.create(body()))

    @app.post("/api/schedules/<rule_id>/enabled")
    @mutating
    def schedules_enabled(rule_id):
        enabled = body().get("enabled")
        if not isinstance(enabled, bool):
            raise ApiError("'enabled' must be true or false")
        return jsonify(rule=scheduler.set_enabled(rule_id, enabled))

    @app.post("/api/schedules/<rule_id>/run")
    @mutating
    def schedules_run(rule_id):
        return jsonify(result=scheduler.run_now(rule_id))

    @app.post("/api/schedules/<rule_id>/delete")
    @mutating
    def schedules_delete(rule_id):
        scheduler.delete(rule_id)
        return jsonify(ok=True)

    return app
