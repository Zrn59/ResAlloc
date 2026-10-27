"""Built-in providers. Importing this module registers them."""

from __future__ import annotations

import importlib
import os
from pathlib import Path

from resalloc.providers.base import Provider
from resalloc.providers.kaggle import _factory as kaggle_factory
from resalloc.providers.laptop import _factory as laptop_factory
from resalloc.providers.machine import _factory as machine_factory
from resalloc.providers.modal_backend import _factory as modal_factory
from resalloc.providers.registry import build_registered, register_provider_if_absent
from resalloc.providers.runpod import _factory as runpod_factory
from resalloc.providers.vast import _factory as vast_factory

register_provider_if_absent("runpod", runpod_factory)
register_provider_if_absent("modal", modal_factory)
register_provider_if_absent("kaggle", kaggle_factory)
register_provider_if_absent("vast", vast_factory)
register_provider_if_absent("machine", machine_factory)
register_provider_if_absent("laptop", laptop_factory)


def build_default(root: Path) -> dict[str, Provider]:
    extra = os.environ.get("RESALLOC_PROVIDER_MODULE")
    if extra:
        importlib.import_module(extra)
    return build_registered(root)
