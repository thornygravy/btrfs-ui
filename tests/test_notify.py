"""Webhook notifications: the sender, the scrub watcher and where they are hooked in."""
# ruff: noqa: F811  (the fixtures come from the other test files)
import http.server
import json
import threading
import time

import pytest

from app import notify
from app.config import Config
from test_api import H, make_client, pool  # noqa: F401
from test_schedules import client, create, sched, srunner  # noqa: F401

URL = "http://ha.local:8123/api/webhook/SECRET-ID"


# ---- configuration -----------------------------------------------------------------

def test_webhook_is_off_by_default():
    cfg = Config({"BTRFS_UI_AUTH": "off"})
    assert cfg.webhook_url is None and cfg.webhook_scrub_ok is False
    assert not notify.Notifier(cfg, None).enabled


@pytest.mark.parametrize("url", ["ha.local:8123/api/webhook/x", "ftp://ha.local/x", "http://", "nonsense"])
def test_a_bad_webhook_address_stops_the_start(url):
    with pytest.raises(SystemExit) as e:
        Config({"BTRFS_UI_AUTH": "off", "BTRFS_UI_WEBHOOK_URL": url}).validate()
    assert "BTRFS_UI_WEBHOOK_URL" in str(e.value)


def test_good_webhook_addresses_pass():
    for url in (URL, "https://ha.example.org/api/webhook/x"):
        Config({"BTRFS_UI_AUTH": "off", "BTRFS_UI_WEBHOOK_URL": url}).validate()


def test_host_leaves_out_the_secret_part():
    n = notify.Notifier(Config({"BTRFS_UI_WEBHOOK_URL": URL}), None)
    assert n.host == "ha.local:8123"
    n = notify.Notifier(Config({"BTRFS_UI_WEBHOOK_URL": "https://ha.example.org/api/webhook/x"}), None)
    assert n.host == "ha.example.org"


# ---- sending, against a real local web server ----------------------------------------

class Receiver(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.got.append((self.path, self.headers["Content-Type"], json.loads(body)))
        self.send_response(self.server.answer)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def receiver(monkeypatch):
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    srv = http.server.HTTPServer(("127.0.0.1", 0), Receiver)
    srv.got, srv.answer = [], 200
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def live(srv):
    url = f"http://127.0.0.1:{srv.server_port}/api/webhook/SECRET-ID"
    return notify.Notifier(Config({"BTRFS_UI_WEBHOOK_URL": url}), None)


def test_send_posts_the_documented_json(receiver):
    live(receiver).send("scrub_errors", "A title", "A message", "tank")
    assert receiver.got == [("/api/webhook/SECRET-ID", "application/json", {
        "event": "scrub_errors", "title": "A title", "message": "A message", "filesystem": "tank"})]


def test_an_error_answer_is_reported_without_the_address(receiver):
    receiver.answer = 404
    with pytest.raises(notify.NotifyError) as e:
        live(receiver).send("test", "t", "m")
    assert "HTTP 404" in str(e.value) and "SECRET" not in str(e.value)


def test_an_unreachable_webhook_is_reported_without_the_address(receiver):
    n = live(receiver)
    receiver.shutdown()
    receiver.server_close()
    with pytest.raises(notify.NotifyError) as e:
        n.send("test", "t", "m")
    assert "Could not reach" in str(e.value) and "SECRET" not in str(e.value)


def test_notify_does_nothing_when_off():
    n = notify.Notifier(Config({}), None, post=lambda *a: pytest.fail("sent"))
    n.notify("test", "t", "m")


# ---- watching scrubs -------------------------------------------------------------------

class FakeFs:
    id, label, uuid = "fs1", "tank", "u-1"

    class primary:
        mount_point = "/mnt/tank"


class FakeRegistry:
    def all(self):
        return [FakeFs()]


def scrub(status, started="Mon Nov 23 04:00:00 2026", errors="no errors found"):
    pairs = [["Scrub started", started], ["Status", status]]
    if errors is not None:
        pairs.append(["Error summary", errors])
    return {"pairs": pairs, "running": status == "running", "status": status}


class Watch:
    """A notifier whose scrub status the test controls and whose messages it collects."""

    def __init__(self, **env):
        self.current = None
        self.events = []
        self.n = notify.Notifier(Config({"BTRFS_UI_WEBHOOK_URL": URL, **env}), FakeRegistry(),
                                 scrub_status=lambda f: self.current)
        self.n.notify = lambda *args: self.events.append(args)

    def look(self, summary):
        self.current = summary
        self.n.check_scrubs()
        return self.events


def test_nothing_is_reported_on_the_first_look():
    w = Watch()
    assert w.look(scrub("finished", errors="csum=3")) == []


def test_a_scrub_that_finishes_with_errors_is_reported_once():
    w = Watch()
    w.look(scrub("finished"))
    w.look(scrub("running", started="Mon Nov 23 05:00:00 2026", errors=None))
    w.look(scrub("finished", started="Mon Nov 23 05:00:00 2026", errors="read=0 csum=3 corrected=3"))
    w.look(scrub("finished", started="Mon Nov 23 05:00:00 2026", errors="read=0 csum=3 corrected=3"))
    assert len(w.events) == 1
    event, title, message, fs = w.events[0]
    assert event == "scrub_errors" and fs == "tank"
    assert "tank" in title and "csum=3" in message


def test_the_message_says_what_was_corrected_and_what_was_not():
    def finish(uncorrectable):
        w = Watch()
        w.look(scrub("running", errors=None))
        done = scrub("finished", errors="csum=1536")
        done["pairs"] += [["Corrected", "1536"], ["Uncorrectable", uncorrectable], ["Unverified", "0"]]
        w.look(done)
        return w.events[0]

    _, title, message, _ = finish("0")
    assert "uncorrectable" not in title
    assert message.endswith("csum=1536 (corrected 1536, uncorrectable 0, unverified 0).")
    _, title, message, _ = finish("12")
    assert title == "Scrub found uncorrectable errors on tank"
    assert "uncorrectable 12" in message


def test_a_clean_scrub_is_quiet_unless_asked_for():
    w = Watch()
    w.look(scrub("running", errors=None))
    assert w.look(scrub("finished")) == []

    w = Watch(BTRFS_UI_WEBHOOK_SCRUB_OK="1")
    w.look(scrub("running", errors=None))
    w.look(scrub("finished"))
    assert [e[0] for e in w.events] == ["scrub_ok"]


def test_a_scrub_that_was_never_seen_running_is_still_reported():
    w = Watch()
    w.look(scrub("finished", started="Mon Nov 23 04:00:00 2026"))
    w.look(scrub("finished", started="Mon Nov 23 06:00:00 2026", errors="csum=1 uncorrectable=1"))
    assert [e[0] for e in w.events] == ["scrub_errors"]


def test_a_cancelled_or_interrupted_scrub_is_not_a_result():
    for status in ("aborted", "interrupted"):
        w = Watch()
        w.look(scrub("running", errors=None))
        assert w.look(scrub(status, errors="csum=1")) == []


def test_a_filesystem_that_never_had_a_scrub_stays_quiet():
    w = Watch()
    never = {"pairs": [], "running": False, "status": "never run"}
    w.look(never)
    assert w.look(never) == []


def test_a_failing_status_command_does_not_stop_the_watch():
    n = notify.Notifier(Config({"BTRFS_UI_WEBHOOK_URL": URL}), FakeRegistry(),
                        scrub_status=lambda f: 1 / 0)
    n.check_scrubs()


# ---- the test button and the session ------------------------------------------------------

def test_test_message_endpoint(pool):
    sent = []
    c = make_client(pool, BTRFS_UI_WEBHOOK_URL=URL)
    c.application.notifier._post = lambda url, payload: sent.append((url, payload))
    r = c.post("/api/notify/test", json={}, headers=H)
    assert r.status_code == 200
    assert sent[0][0] == URL and sent[0][1]["event"] == "test"


def test_test_message_reports_a_failure(pool):
    c = make_client(pool, BTRFS_UI_WEBHOOK_URL=URL)

    def fail(url, payload):
        raise notify.NotifyError("The webhook answered HTTP 404")

    c.application.notifier._post = fail
    r = c.post("/api/notify/test", json={}, headers=H)
    assert r.status_code == 502 and "HTTP 404" in r.get_json()["error"]


def test_test_message_needs_a_webhook(pool):
    r = make_client(pool).post("/api/notify/test", json={}, headers=H)
    assert r.status_code == 400 and "BTRFS_UI_WEBHOOK_URL" in r.get_json()["error"]


def test_test_message_works_in_read_only_mode(pool):
    c = make_client(pool, BTRFS_UI_WEBHOOK_URL=URL, BTRFS_UI_READONLY="1")
    c.application.notifier._post = lambda url, payload: None
    assert c.post("/api/notify/test", json={}, headers=H).status_code == 200


def test_session_shows_the_host_only_to_signed_in_users(pool):
    c = make_client(pool, BTRFS_UI_AUTH="on", BTRFS_UI_PASSWORD="s3cret", BTRFS_UI_WEBHOOK_URL=URL)
    assert c.get("/api/session").get_json()["webhook"] is None
    c.post("/api/login", json={"password": "s3cret"}, headers=H)
    raw = c.get("/api/session").get_data(as_text=True)
    assert json.loads(raw)["webhook"] == "ha.local:8123"
    assert "SECRET" not in raw


# ---- schedules tell the notifier when they fail ------------------------------------------------

def test_a_failed_scheduled_run_is_reported(client, srunner):
    events = []
    create(client)
    s = sched(client)
    s._notify = lambda *args: events.append(args)
    rule = s.store.rules[0]
    rule["source"] = "gone"
    s._execute(rule, "schedule", time.time(), None)
    assert len(events) == 1
    event, title, message, fs = events[0]
    assert event == "schedule_failed" and title.startswith("Snapshot of gone failed")
    assert "no longer exists" in message and fs


def test_manual_runs_and_good_runs_are_not_reported(client, srunner):
    events = []
    create(client)
    s = sched(client)
    s._notify = lambda *args: events.append(args)
    rule = s.store.rules[0]
    s._execute(rule, "schedule", time.time(), None)   # a good scheduled run
    rule["source"] = "gone"
    s.run_now(rule["id"])                              # fails, but someone is watching
    assert events == []
