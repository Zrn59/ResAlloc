# Job and script

**English** · [Русский](../job.md)

Code: `src/resalloc/models.py`, `src/resalloc/worker.py`, `src/resalloc/policy.py`.

`Job` is the script, the start provider, `failover`, and the datasets. A string in `datasets` means `route="hub"`. A `Dataset` object picks the road.

`Limits` decides when to ask for a save (`soft_buffer`) and when to cut the run even if the script does not answer (`hard_buffer`).

| Field | Meaning |
| --- | --- |
| `runpod_min_balance_usd` | do not spend the balance below this, when the API returns one |
| `runpod_max_spend_usd` | session cap when there is no live balance |
| `runpod_max_hourly_usd` | do not take a GPU above this hourly price |
| `modal_max_spend_usd` | cap on `metered_cost`, before credits, not "what is left of $30" |
| `kaggle_min_gpu_hours` | hours of GPU/TPU to keep in reserve |
| `vast_min_balance_usd` | do not spend the Vast balance below this |
| `vast_max_spend_usd` | Vast session cap |
| `vast_max_hourly_usd` | do not take an offer above this hourly price |
| `laptop_heartbeat_timeout` | how long a silent laptop counts as down |

`data_timeout_seconds` is on `Job`, not on `Limits`: how long to wait for SSH before a direct dataset is given up. The script then writes a non-zero `finished.json`, so the GPU is not held with no data.

A non-zero script exit is `STOP` with reason `worker_failed`. A vanished pod, a lost laptop heartbeat, and several failed probes in a row still move the job when a next step exists.

The same code runs on every provider:

```python
worker = Worker()
state = worker.load()
worker.on_stop(save_weights)   # runs before the ack
worker.save({"epoch": epoch})
signal = worker.pulse()        # continue / checkpoint_and_migrate / stop
for path in worker.iter_ready("huge"):
    ...
worker.wait_data("huge")       # until COMPLETE appears
```

`worker.path("model.pt")` is a file the hub picks up with the checkpoint.

Policy: plenty of headroom is `CONTINUE`. Headroom inside the soft buffer is `MIGRATE`, or `PARK` when the next step asks for confirmation. The hard buffer or a dead workload does not wait for an ack. A clean finish is `STOP` with reason `completed`, and the job is not moved.
