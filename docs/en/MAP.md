# Project map

**English** · [Русский](../MAP.md)

```
src/resalloc/
  __init__.py          public names: Job, Dataset, ResAlloc, Worker, providers
  models.py            Job, Limits, Dataset, RunPod, Vast, Modal, Kaggle, Machine, Laptop
  client.py            submit a job from the laptop
  data.py              chunks, DirectorySink, keep/store choice
  data_stores.py       direct upload to Modal, Kaggle, Runpod, Vast, and SSH
  worker.py            the API imported by train.py
  policy.py            CONTINUE / MIGRATE / PARK / STOP
  hub.py               daemon: queue, thresholds, move, checkpoint
  layout.py            directories on the hub disk
  names.py             safe job ids, dataset names, and hashes
  records.py           journal.json and confirm/edit/resume/data_ready
  transport.py         local disk or SSH+rsync
  config.py            ~/.config/resalloc/config.toml
  setup.py             doctor, init, install, bootstrap, attach
  laptop_agent.py      the laptop dials the hub
  cli.py               the resalloc command
  providers/
    base.py            adapter protocol and RunningJob
    registry.py        register_provider
    runpod.py          REST v2
    vast.py            Vast.ai REST and SSH
    machine.py         SSH to a host you already have
    modal_backend.py   Sandbox + Volume + billing
    kaggle.py          quota, kernels, datasets
    laptop.py          heartbeat and the local process
deploy/resalloc-hub.service
docs/                  Russian notes
docs/en/               these notes
tests/                 no keys and no live VM
```

Control always goes the same way: the laptop drops a small request on the hub, the hub calls the APIs and keeps the journal. Dataset bytes are a separate choice and can skip the hub. After a stop, the checkpoint is still pulled back to the hub.

Hub disk: `inbox/`, `jobs/<id>/` (`bundle`, `runtime`, `checkpoints`, `logs`, `journal.json`), `cache/datasets/<hash>/`, `agents/laptop.json`.
