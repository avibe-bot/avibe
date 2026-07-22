class HarnessError(RuntimeError):
    """A redaction-safe POC harness failure."""


class ConfigurationError(HarnessError):
    """The owner-supplied provider configuration cannot support a live run."""


class LaunchError(HarnessError):
    """The owned EverOS sidecar could not be launched safely."""


class ReportValidationError(HarnessError):
    """A report does not satisfy the frozen POC schema."""


class StageNotImplementedError(HarnessError):
    """A frozen CLI stage is reserved for a later POC delivery."""
