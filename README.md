# resalloc

<div align="center">

**English** · [Русский](README.ru.md) · [Docs](docs/en/README.md) · [Документация](docs/README.md)

<br>

[![zrn59.me](https://img.shields.io/badge/⚡_Powered_by-zrn59.me-f59e0b?style=for-the-badge&labelColor=18181b)](https://zrn59.me)
[![Donate](https://img.shields.io/badge/☕_Support_/_Donate-ff5f57?style=for-the-badge&labelColor=18181b)](https://www.donationalerts.com/r/zrn59)

*Custom Bot & Web Development • Data Parsing • Isolated Docker Hosting — **[zrn59.me](https://zrn59.me)***

</div>

---

One training script, several GPU providers. A small always-on hub watches the money and the quota, asks the script to save, and moves the job before the limit. The hub does not train the model. Checkpoints and the journal stay on the hub. A large dataset can go straight to the provider and skip the hub disk.

Built-in providers:

| Provider | How the hub reaches it | What limits it |
| --- | --- | --- |
| Runpod | REST v2, then SSH | balance if the API returns one, otherwise `runpod_max_spend_usd` |
| Vast.ai | REST, then SSH | account balance and `vast_max_spend_usd` |
| Modal | Sandbox and Volume | `modal_max_spend_usd` against metered cost, before credits |
| Kaggle | official CLI | accelerator hours, with the session lease baked in at push |
| Machine | SSH to a host you already have | free; stopping the job does not power the machine off |
| Laptop | the laptop dials the hub | free, and it has to keep heartbeating |

Anything else is a provider object plus `register_provider`. Set `RESALLOC_PROVIDER_MODULE` and the hub imports it at startup.

## Install

Python 3.11 or newer, on Linux or macOS. A hub on another machine also needs OpenSSH and rsync. Nothing here assumes a particular host, user, or cloud account.

```bash
pip install git+https://github.com/Zrn59/ResAlloc.git
resalloc help
```

That installs the current `main`. The release tag is `git+https://github.com/Zrn59/ResAlloc.git@v0.2.4`. Changes are in [CHANGELOG.md](CHANGELOG.md).

Two setups:

```bash
# this machine is the hub
resalloc up

# this laptop submits jobs, NAME in ~/.ssh/config is the hub
resalloc up --ssh-host NAME
```

`resalloc up` writes `~/.config/resalloc/config.toml`. Locally the hub directory is `~/resalloc`. With `--ssh-host` the directory is `~/resalloc` on that host, the package is copied there, and the daemon starts when that account has systemd. Nothing else is downloaded. Modal and Kaggle, if you use them, are `resalloc up --ssh-host NAME --extras all`.

Then put provider keys in the hub's `secrets.env` and run `resalloc doctor`. `resalloc config` shows the laptop, the hub, the disk reserve, the caps, and the names of secrets. It does not print secret values. Spend caps for every job go in `[limits]` in `config.toml`; a value set on the job itself wins. `resalloc --help` lists every command. The short notes are [docs/SHORT.md](docs/SHORT.md) and [docs/en/SHORT.md](docs/en/SHORT.md).

The core package has no dependencies. A laptop-only job, or a job on an SSH machine you already have, needs no API keys. On a smaller disk the free-space reserve is 1 GiB; on a large disk it stays 20 GiB. Runpod and Vast use the standard library. Modal and Kaggle need `pip install "resalloc[all]"` on the machine that calls their APIs.

Keys stay in the hub's `secrets.env` (`chmod 600`), not in the repo:

- `RUNPOD_API_KEY`
- `VAST_API_KEY` (register the hub account's SSH public key on Vast before the first start)
- Modal token (`MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`)
- `KAGGLE_USERNAME` and `KAGGLE_API_TOKEN`

## A job

```python
from datetime import timedelta
from resalloc import Job, Laptop, Limits, Machine, Migrate, Modal, ResAlloc, RunPod, Vast

job = Job(
    script="train.py",
    requirements="requirements.txt",
    datasets=["./data/big"],
    limits=Limits(
        soft_buffer=timedelta(minutes=15),
        hard_buffer=timedelta(minutes=5),
        runpod_max_spend_usd=5,
        vast_max_spend_usd=5,
        modal_max_spend_usd=25,
    ),
    start=RunPod(gpu="NVIDIA GeForce RTX 4090", spot=True),
    failover=[
        Migrate(Vast(gpu="RTX 4090"), confirm=True),
        Migrate(Modal(gpu="A10G"), confirm=False),
        Migrate(Machine(host="lab.example", user="trainer"), confirm=False),
        Migrate(Laptop(), confirm=False),
    ],
)
ResAlloc.submit(job)
```

The hub folder is named from the script and the local time, `jobs/train-2026-10-31-18-11-a3f9`. `name="city"` on the `Job` replaces `train`. `checkpoints` inside it is the intermediate state, `logs` is the output.

`confirm=True` does not leave a paid GPU running while the laptop is closed. The hub takes a checkpoint, releases the machine, and waits:

```bash
resalloc status
resalloc confirm JOB
resalloc resume JOB
```

`resalloc attach JOB` asks in the terminal. `resalloc submit job.json` takes the same fields as `Job`. `JOB` in every command is that folder name. When the job starts waiting for confirm, the hub can POST once to `notify_url` or run `notify_command` from `hub.toml`. Details are in [docs/en/hub.md](docs/en/hub.md).

The same `train.py` runs everywhere:

```python
from resalloc.worker import Worker

worker = Worker()
state = worker.load()

def flush():
    path = worker.path("model.pt")
    # write weights to path

worker.on_stop(flush)
for epoch in range(state.get("epoch", 0), 10):
    worker.save({"epoch": epoch + 1})
    signal = worker.pulse()
    if signal.value != "continue":
        break
```

`pulse()` returns `continue`, `checkpoint_and_migrate`, or `stop`. Put the heavy save in `on_stop`: it runs before the ack. A non-zero exit is `worker_failed`. The hub stops the chain instead of paying the next provider to hit the same bug. Resume after the script is fixed. A disappeared pod, a lost laptop heartbeat, or a provider API that stays down still moves the job.

On the laptop:

```bash
resalloc laptop agent
```

The agent dials out. The hub does not connect in through NAT. If the start provider is the laptop and there is no heartbeat, submit fails.

## Many jobs

One hub process watches every job. `RESALLOC_TICK_WORKERS` (default 1, the systemd unit sets 4) probes several jobs at once. Starts stay serialized so two ticks cannot rent the same slot twice.

Caps live in `hub.toml` on the hub, and environment variables override the file:

```toml
max_running = 8
tick_workers = 4
probe_failures = 5

[providers.runpod]
max_running = 2

[providers.vast]
max_running = 2
```

`max_running` at the top is the total number of running jobs. A provider table entry is that provider's own cap. A job that does not fit stays `queued` with reason `at_capacity` and is tried again on the next tick. After `probe_failures` failed probes the hub treats the workload as gone, so a stuck API cannot leave a GPU billing.

The hub disk is shared with the OS. A new hub writes `reserve_bytes` once: 20 GiB when at least 80 GiB is free, 1 GiB when at least 8 GiB is free, otherwise a tenth of the free space and at least 64 MiB. `RESALLOC_RESERVE_BYTES` overrides that number. An existing `hub.toml` is left as it is. Dataset cache is evicted by age, except hashes that a live job still uses. A new upload is refused at the watermark. A running job can still write its checkpoint. A refused job keeps its bundle, so `resalloc resume` can try again after you free space.

## Data

A repeated dataset is recognized by a content hash and is not uploaded twice.

- `hub` copies it onto the hub. Use this for small sets.
- `direct` sends it from the submitting machine to the provider, one file at a time.
- `stream` does the same in 8 MiB chunks and marks each finished file, so training can read it early.

```python
from resalloc import Dataset, Job, Modal, ResAlloc

ResAlloc.submit(Job(
    script="train.py",
    start=Modal(gpu="A10G", volume="datasets"),
    datasets=[Dataset("./huge", route="direct", keep="modal", store="datasets")],
))
```

Direct bytes for Modal and Kaggle are uploaded before the GPU starts. Runpod, Vast, and a machine need an SSH endpoint first, so that upload waits for the hub to publish one. The training process stops waiting if the upload does not finish within `data_timeout_seconds` (default 3600), and the hub then releases the GPU.

## License

MIT. See [LICENSE](LICENSE). Provider API keys never belong in the repository.
