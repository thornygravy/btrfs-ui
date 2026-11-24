"""Runtime configuration, read from environment variables."""
import os
import secrets
from urllib.parse import urlsplit


PLACEHOLDER_PASSWORDS = {"change-me", "changeme", "password"}


def _kept_secret(data_dir):
    """A random key saved as <data_dir>/secret (readable only by us). Nothing is created
    when the folder is not there, so a missing /data volume still means "schedules off"."""
    path = os.path.join(data_dir, "secret")

    def read():
        try:
            with open(path, encoding="ascii") as fh:
                value = fh.read().strip()
            return value if len(value) >= 32 else None
        except (OSError, UnicodeError):
            return None

    value = read()
    if value:
        return value
    value = secrets.token_hex(32)
    if os.path.isdir(data_dir):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as fh:
                fh.write(value + "\n")
        except FileExistsError:
            return read() or value
        except OSError:
            pass  # read-only or full: this run just uses a random key
    return value


def _flag(value, default=False):
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


class Config:
    def __init__(self, env=None):
        e = os.environ if env is None else env

        self.host = e.get("BTRFS_UI_HOST", "0.0.0.0")
        self.port = int(e.get("BTRFS_UI_PORT", "8008"))

        # Authentication. A password is required unless auth is switched off.
        self.auth_enabled = e.get("BTRFS_UI_AUTH", "on").strip().lower() not in (
            "off", "0", "false", "no")
        password = e.get("BTRFS_UI_PASSWORD")
        password_file = e.get("BTRFS_UI_PASSWORD_FILE")
        if not password and password_file:
            with open(password_file, encoding="utf-8") as fh:
                password = fh.read().strip()
        self.password = password or None

        self.cookie_secure = _flag(e.get("BTRFS_UI_COOKIE_SECURE"))

        # View everything, change nothing.
        self.readonly = _flag(e.get("BTRFS_UI_READONLY"))

        # Optional: pass `--root <path>` to snapper (experimental).
        self.snapper_root = e.get("BTRFS_UI_SNAPPER_ROOT") or None

        # Where schedules are stored. Scheduling stays off until this directory
        # exists, i.e. until a volume is mounted there.
        self.data_dir = e.get("BTRFS_UI_DATA_DIR", "/data")

        # Signing key for the session cookie: the one provided, else one kept in the
        # data folder so logins survive a restart, else a random one for this run.
        self.secret = e.get("BTRFS_UI_SECRET") or _kept_secret(self.data_dir)

        # Optional: an address that gets a JSON message when a scrub finds errors or a
        # schedule fails (for example a Home Assistant webhook).
        self.webhook_url = (e.get("BTRFS_UI_WEBHOOK_URL") or "").strip() or None
        self.webhook_scrub_ok = _flag(e.get("BTRFS_UI_WEBHOOK_SCRUB_OK"))

        # Where to read the mount table from (only changed by the tests).
        self.mountinfo = e.get("BTRFS_UI_MOUNTINFO", "/proc/self/mountinfo")

    def validate(self):
        if self.webhook_url:
            u = urlsplit(self.webhook_url)
            if u.scheme not in ("http", "https") or not u.hostname:
                raise SystemExit("BTRFS_UI_WEBHOOK_URL must be a full address starting with "
                                 "http:// or https://")
        if self.auth_enabled and self.password in PLACEHOLDER_PASSWORDS:
            raise SystemExit("BTRFS_UI_PASSWORD is still the example value. Pick your own.")
        if self.auth_enabled and not self.password:
            raise SystemExit(
                "Refusing to start without a password.\n"
                "Set BTRFS_UI_PASSWORD (or BTRFS_UI_PASSWORD_FILE), or set "
                "BTRFS_UI_AUTH=off if something else in front of this "
                "container already handles authentication."
            )
