"""Staying signed in: the checkbox, expiry, a changed password, and the kept signing key."""
# ruff: noqa: F811  (the fixtures come from the other test files)
import os
import stat
import time

from app.config import Config
from test_api import H, make_client, pool  # noqa: F401

DAY = 24 * 3600


def secure(pool, password="s3cret", **env):
    return make_client(pool, BTRFS_UI_AUTH="on", BTRFS_UI_PASSWORD=password,
                       BTRFS_UI_DATA_DIR="/nonexistent", **env)


def login(c, password="s3cret", **extra):
    return c.post("/api/login", json={"password": password, **extra}, headers=H)


def state(c):
    with c.session_transaction() as s:
        return dict(s)


def signed_in(c):
    return c.get("/api/session").get_json()["authenticated"]


def test_a_normal_sign_in_lasts_twelve_hours_from_the_last_request(pool):
    c = secure(pool)
    assert login(c).status_code == 200
    assert state(c)["ttl"] == 12 * 3600
    assert signed_in(c)


def test_stay_signed_in_lasts_thirty_days(pool):
    c = secure(pool)
    assert login(c, remember=True).status_code == 200
    s = state(c)
    assert s["ttl"] == 30 * DAY
    assert 30 * DAY - 60 < s["exp"] - time.time() <= 30 * DAY


def test_only_a_real_true_means_remember(pool):
    for value in ("true", 1, "yes", None):
        c = secure(pool)
        login(c, remember=value)
        assert state(c)["ttl"] == 12 * 3600


def test_a_wrong_password_signs_nobody_in_even_with_the_box_ticked(pool):
    c = secure(pool)
    assert login(c, password="nope", remember=True).status_code == 401
    assert not signed_in(c)


def test_an_expired_session_is_signed_out(pool):
    c = secure(pool)
    login(c)
    with c.session_transaction() as s:
        s["exp"] = time.time() - 1
    assert not signed_in(c)
    assert c.get("/api/filesystems").status_code == 401


def test_every_request_moves_the_expiry_forward(pool):
    c = secure(pool)
    login(c)
    with c.session_transaction() as s:
        s["exp"] = time.time() + 10
    assert c.get("/api/filesystems").status_code == 200
    assert state(c)["exp"] > time.time() + 12 * 3600 - 60


def test_a_session_from_before_this_change_has_to_sign_in_again(pool):
    c = secure(pool)
    with c.session_transaction() as s:
        s["auth"] = True  # the old cookie held nothing else
    assert not signed_in(c)


def test_changing_the_password_signs_everybody_out(pool):
    first = secure(pool, BTRFS_UI_SECRET="k" * 40)
    login(first, remember=True)
    second = secure(pool, password="a new password", BTRFS_UI_SECRET="k" * 40)
    with second.session_transaction() as s:
        s.update(state(first))
    assert not signed_in(second)
    # while the same password with the same key still accepts it
    third = secure(pool, BTRFS_UI_SECRET="k" * 40)
    with third.session_transaction() as s:
        s.update(state(first))
    assert signed_in(third)


def test_logging_out_clears_the_session(pool):
    c = secure(pool)
    login(c, remember=True)
    c.post("/api/logout", json={}, headers=H)
    assert not signed_in(c)


# ---- the signing key survives a restart when there is a data folder ----------------

def test_the_key_is_kept_in_the_data_folder(tmp_path):
    env = {"BTRFS_UI_DATA_DIR": str(tmp_path)}
    key = Config(env).secret
    assert len(key) >= 32
    assert Config(env).secret == key
    path = tmp_path / "secret"
    assert path.read_text().strip() == key
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_a_provided_key_wins_and_nothing_is_written(tmp_path):
    cfg = Config({"BTRFS_UI_DATA_DIR": str(tmp_path), "BTRFS_UI_SECRET": "mine"})
    assert cfg.secret == "mine"
    assert not (tmp_path / "secret").exists()


def test_without_a_data_folder_the_key_is_random_and_nothing_is_created(tmp_path):
    missing = tmp_path / "not-mounted"
    env = {"BTRFS_UI_DATA_DIR": str(missing)}
    assert Config(env).secret != Config(env).secret
    assert not missing.exists()


def test_a_short_or_damaged_key_file_is_replaced(tmp_path):
    (tmp_path / "secret").write_text("short")
    key = Config({"BTRFS_UI_DATA_DIR": str(tmp_path)}).secret
    assert len(key) >= 32 and key != "short"


def test_a_data_folder_that_cannot_be_written_still_starts(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise PermissionError

    monkeypatch.setattr(os, "open", refuse)
    assert len(Config({"BTRFS_UI_DATA_DIR": str(tmp_path)}).secret) >= 32
