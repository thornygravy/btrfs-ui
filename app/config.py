"""Runtime configuration, read from environment variables."""
import os
import secrets


PLACEHOLDER_PASSWORDS = {"change-me", "changeme", "password"}


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

        # Signing key for the session cookie. Random per start unless provided,
        # which just means a restart signs everybody out.
        self.secret = e.get("BTRFS_UI_SECRET") or secrets.token_hex(32)
        self.cookie_secure = _flag(e.get("BTRFS_UI_COOKIE_SECURE"))

        # View everything, change nothing.
        self.readonly = _flag(e.get("BTRFS_UI_READONLY"))

        # Optional: pass `--root <path>` to snapper (experimental).
        self.snapper_root = e.get("BTRFS_UI_SNAPPER_ROOT") or None

        # Where to read the mount table from (only changed by the tests).
        self.mountinfo = e.get("BTRFS_UI_MOUNTINFO", "/proc/self/mountinfo")

    def validate(self):
        if self.auth_enabled and self.password in PLACEHOLDER_PASSWORDS:
            raise SystemExit("BTRFS_UI_PASSWORD is still the example value. Pick your own.")
        if self.auth_enabled and not self.password:
            raise SystemExit(
                "Refusing to start without a password.\n"
                "Set BTRFS_UI_PASSWORD (or BTRFS_UI_PASSWORD_FILE), or set "
                "BTRFS_UI_AUTH=off if something else in front of this "
                "container already handles authentication."
            )
