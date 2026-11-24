"""Optional webhook messages, for example to Home Assistant.

Set BTRFS_UI_WEBHOOK_URL and btrfs-ui sends a small JSON document to that
address when something needs attention:

    {"event": "scrub_errors", "title": "...", "message": "...", "filesystem": "tank"}

Events: scrub_errors, schedule_failed, scrub_ok (only with
BTRFS_UI_WEBHOOK_SCRUB_OK=1) and test (the button in Settings).

The URL usually contains a secret (Home Assistant's webhook id), so it is
never logged or sent to the browser; only its host is.
"""

import json
import logging
import threading
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from . import btrfs, proc

log = logging.getLogger("btrfs-ui")
B = btrfs.BTRFS


class NotifyError(Exception):
    pass


def _post(url, payload, timeout=10):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "btrfs-ui"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(1024)
    except urllib.error.HTTPError as e:
        raise NotifyError(f"The webhook answered HTTP {e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        raise NotifyError("Could not reach the webhook: " + str(getattr(e, "reason", e))) from None


def _scrub_status(f):
    rc, out, err = proc.run([B, "scrub", "status", f.primary.mount_point], check=False)
    return btrfs.scrub_summary(out or err)


class Notifier:
    def __init__(self, cfg, registry, post=None, scrub_status=None):
        self.url = cfg.webhook_url
        self.scrub_ok = cfg.webhook_scrub_ok
        self.registry = registry
        self._post = post or _post
        self._scrub_status = scrub_status or _scrub_status
        self._seen = {}  # filesystem id -> (scrub running, "Scrub started" text)
        self._thread = None

    @property
    def enabled(self):
        return bool(self.url)

    @property
    def host(self):
        """Where messages go, without the secret part of the address."""
        if not self.url:
            return None
        u = urlsplit(self.url)
        return f"{u.hostname}:{u.port}" if u.port else u.hostname

    # -- sending --

    def send(self, event, title, message, filesystem=None):
        """Send one message and wait for the answer. Raises NotifyError."""
        self._post(self.url, {"event": event, "title": title,
                              "message": message, "filesystem": filesystem})

    def notify(self, event, title, message, filesystem=None):
        """Send in the background. A message that cannot be delivered is logged."""
        if not self.enabled:
            return

        def work():
            try:
                self.send(event, title, message, filesystem)
            except NotifyError as e:
                log.warning("Webhook message '%s' was not delivered: %s", event, e)

        threading.Thread(target=work, name="webhook", daemon=True).start()

    # -- watching scrubs --

    def start(self, interval=60):
        if not self.enabled or self._thread:
            return

        def loop():
            while True:
                try:
                    self.check_scrubs()
                except Exception:
                    log.exception("scrub check failed")
                threading.Event().wait(interval)

        self._thread = threading.Thread(target=loop, name="scrub-watch", daemon=True)
        self._thread.start()
        log.info("Webhook notifications go to %s", self.host)

    def check_scrubs(self):
        """Look at every filesystem once and report scrubs that finished since the last look.

        Nothing is reported on the first look, so a restart does not repeat old results.
        """
        for f in self.registry.all():
            try:
                summary = self._scrub_status(f)
            except Exception:
                log.exception("could not read the scrub status of %s", f.label)
                continue
            self._scrub_seen(f, summary)

    def _scrub_seen(self, f, summary):
        pairs = dict(summary["pairs"])
        started = pairs.get("Scrub started")
        before = self._seen.get(f.id)
        self._seen[f.id] = (summary["running"], started)
        if before is None or summary["running"]:
            return
        was_running, started_before = before
        # Finished while we watched, or so quickly that it was never seen running.
        if not (was_running or started != started_before):
            return
        if summary["status"] != "finished":
            return  # cancelled or interrupted, not a result
        errors = pairs.get("Error summary")
        if errors is None:
            return
        name = f.label or f.primary.mount_point
        if errors.strip().lower() == "no errors found":
            if self.scrub_ok:
                self.notify("scrub_ok", f"Scrub finished on {name}",
                            f"The scrub on {name} finished with no errors.", name)
        else:
            counts = [f"{k.lower()} {pairs[k]}" for k in ("Corrected", "Uncorrectable", "Unverified")
                      if k in pairs]
            bad = pairs.get("Uncorrectable", "0").strip() not in ("0", "")
            self.notify("scrub_errors", f"Scrub found {'uncorrectable ' if bad else ''}errors on {name}",
                        f"The scrub on {name} finished with errors: {errors}"
                        + (f" ({', '.join(counts)})." if counts else "."), name)
