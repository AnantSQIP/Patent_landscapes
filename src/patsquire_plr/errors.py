"""Exception hierarchy.

Every failure the system raises on purpose derives from ``PlrError`` so callers can tell
expected, explained failures apart from bugs. None of these are ever swallowed silently.
"""


class PlrError(Exception):
    """Base class for all deliberate PLR system failures."""


class ConfigError(PlrError):
    """Configuration is missing, malformed, or unsafe (e.g. a secret in a committed file)."""


class HealthCheckError(PlrError):
    """A dependency is reachable but not in the state the system requires."""
