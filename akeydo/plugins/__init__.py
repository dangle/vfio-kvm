import sys

if sys.version_info < (3, 10):
    from importlib_metadata import entry_points
else:
    from importlib.metadata import entry_points

from . import (
    cpu,
    devices,
    memory,
)

__all__ = (
    "cpu",
    "devices",
    "installed_plugins",
    "memory",
)


installed_plugins = entry_points(group=__name__)
