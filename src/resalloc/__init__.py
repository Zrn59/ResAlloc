"""Move one Python job across GPU hosts, and stop it before the money runs out.

The hub keeps checkpoints and the journal. Training code stays the same on
Runpod, Vast.ai, Modal, Kaggle, an SSH machine, and the laptop.
"""

from resalloc.client import ResAlloc
from resalloc.errors import (
    DiskWatermarkError,
    JobStateError,
    LaptopOfflineError,
    ProviderBusy,
    ProviderError,
    ResallocError,
)
from resalloc.models import Dataset, Job, Kaggle, Laptop, Limits, Machine, Migrate, Modal, RunPod, Vast
from resalloc.policy import Signal
from resalloc.providers.registry import register_provider
from resalloc.worker import Worker

__all__ = [
    "Dataset",
    "DiskWatermarkError",
    "Job",
    "JobStateError",
    "Kaggle",
    "Laptop",
    "LaptopOfflineError",
    "Limits",
    "Machine",
    "Migrate",
    "Modal",
    "ProviderBusy",
    "ProviderError",
    "ResAlloc",
    "ResallocError",
    "RunPod",
    "Signal",
    "Vast",
    "Worker",
    "register_provider",
]
