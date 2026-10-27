# Modal

**English** · [Русский](../../providers/modal.md)

Code: `src/resalloc/providers/modal_backend.py`. Dataset upload: `src/resalloc/data_stores.py`.

A long run is `modal.Sandbox`, not a function decorator. The job code lives on a volume named `resalloc-<job>`. A separate persistent volume is `volume`: it is mounted at `/data` and outlives the job. `Dataset(..., keep="modal")` goes there. That storage does not need a GPU, and the bytes do not land on the hub.

| Setting | Where it goes |
| --- | --- |
| `gpu` | `Sandbox.create(gpu=...)` |
| `image` | `Image.from_registry`, otherwise Debian plus `requirements.txt` |
| `cpu`, `memory_mb` | `cpu`, `memory` |
| `timeout_hours` | `timeout` in seconds |
| `volume` | persistent volume at `/data` |
| `region`, `cloud` | the same Sandbox arguments, when set |
| `env` | the environment, together with `RESALLOC_JOB_DIR` |

Money: `Workspace.billing.summary`. The cap is compared with `metered_cost` (the meter before credits). `billed_cost` stays zero while the free $30 covers the spend, so a limit on it would fire too late. When the hourly report has not closed yet, the rate is taken from the first meter sample in this session, not from the previous one-minute poll: Modal updates the meter in batches. There is no "left of $30" field. The cap is `modal_max_spend_usd`.

A direct upload from the laptop needs the `modal` package and a token on the laptop. The hub token is what opens and closes the Sandbox. The stop signal is a file on the code volume, then `terminate`.
