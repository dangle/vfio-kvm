import sys

if sys.version_info < (3, 10):
    from importlib_metadata import entry_points
else:
    from importlib.metadata import entry_points

from . import (
    devices,
)

__all__ = (
    "devices",
    "installed_plugins",
)


installed_plugins = entry_points(group=__name__)
