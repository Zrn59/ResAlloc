# Contributing

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

The core has no required dependencies. `modal` and `kaggle` are imported only when those providers run.

```python
from resalloc import register_provider

register_provider("lab", lambda root: LabAdapter(root))
```

`RESALLOC_PROVIDER_MODULE` is imported when the hub starts. Do not commit API keys, `secrets.env`, or private keys. Tests run without network and without credentials.
