"""The container's clock.

snapper, btrfs and the scheduler all print times in the container's time zone
(set with TZ), so the UI shows every time in that zone too.
"""

import time


def wall(ts):
    """A timestamp as local 'YYYY-MM-DD HH:MM', or None when there is none."""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else None


def zone():
    """The zone name and offset, e.g. 'EST (UTC-0500)'."""
    return time.strftime("%Z (UTC%z)")
