"""Errors the caller can catch."""


class ResallocError(Exception):
    """Base error for resalloc."""


class LaptopOfflineError(ResallocError):
    """The laptop was chosen, and it is not sending a heartbeat."""


class DiskWatermarkError(ResallocError):
    """The hub disk is inside the reserve, so a new upload is refused."""


class JobStateError(ResallocError):
    """The command does not apply to the job's current state."""


class ProviderError(ResallocError):
    """A provider API call failed."""


class ProviderBusy(ResallocError):
    """The provider is already running as many jobs as the hub allows."""

    def __init__(self, provider: str) -> None:
        super().__init__(f"at capacity for {provider}")
        self.provider = provider
