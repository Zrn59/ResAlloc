# Commands

**English** · [Русский](../cli.md)

Code: `src/resalloc/cli.py`.

| Command | Why |
| --- | --- |
| `resalloc` / `resalloc help` | short setup: hub on this machine, or a laptop plus a hosting hub |
| `resalloc config` | laptop target, hub directory, disk reserve, caps, and secret names. Values of keys are not printed |
| `resalloc up` | hub on this machine. `--ssh-host NAME` installs the hub on that `~/.ssh/config` host. `--no-start` is local only |
| `resalloc init` | write `~/.config/resalloc/config.toml`. `--local`, `--ssh-host`, `--hub-root`. An existing file is kept unless `--force` |
| `resalloc submit job.json` | submit a job. The JSON has the same fields as `Job` |
| `resalloc status [JOB]` | state, provider, step, checkpoint freshness, next step |
| `resalloc attach JOB` | wait, then answer `confirm` or `edit gpu=..., spot=true` |
| `resalloc edit JOB --gpu ...` | change the next provider while the job is parked. Also `--volume`, `--region`, `--cloud`, `--max-price`, `--host` |
| `resalloc confirm JOB` | continue after a park |
| `resalloc resume JOB` | start a stopped or failed job again from its checkpoint |
| `resalloc doctor` | check this machine, or the hub behind `ssh_host` |
| `resalloc test modal kaggle` | a one-minute PyTorch run. Providers: `modal`, `kaggle`, `runpod`, `vast`, `laptop`. Modal is capped at $0.10, Runpod and Vast at $0.20. `laptop` starts an agent when there is no heartbeat. Vast does not start without `VAST_API_KEY` in `secrets.env` |
| `resalloc hub install` | prepare the hub directory on this machine. Refused when the config points at an SSH host |
| `resalloc hub bootstrap` | install and start the hub on `ssh_host`. `--extras all` also installs Modal and Kaggle |
| `resalloc hub run` | the daemon loop on this machine |
| `resalloc laptop agent` | this laptop as a free provider |
| `resalloc laptop push JOB` | send the latest checkpoint right after `save` |

`JOB` is the folder name `resalloc submit` prints, for example `train-2026-10-31-18-11-a3f9`. `resalloc config --json` prints the same settings as one object for a script. Secret values are not in it.

`edit` inside `attach` takes `key=value` separated by commas. `spot` and `internet` are booleans. `disk_gb`, `cpus`, `gpu_count`, `memory_mb`, `ram_gb`, `timeout_hours`, `cpu`, and `max_price` are numbers. A GPU name stays a string, and so do `volume`, `region`, `cloud`, and `host`. `--json` on `edit` is an object of fields, not a switch. The flags add to it.
