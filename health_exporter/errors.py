class ExporterError(RuntimeError):
    """Expected, user-actionable exporter failure."""


class RootRequiredError(ExporterError):
    """Raised when a strict source operation requires MuMu Root."""


class VersionMismatchError(ExporterError):
    """Raised when UI adapters have not been qualified for the installed app."""
