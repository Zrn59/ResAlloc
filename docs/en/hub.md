# Hub

**English** · [Русский](../hub.md)

Code: `src/resalloc/hub.py`, `src/resalloc/transport.py`, `src/resalloc/setup.py`, `src/resalloc/config.py`.

The config is `~/.config/resalloc/config.toml`. `resalloc init` writes it. With no `ssh_host`, this machine is the hub and the directory defaults to `~/resalloc`. With `ssh_host`, that name is a `Host` from `~/.ssh/config`: the user, port, and key live there, not in the library.

```toml
ssh_host = "my-server"
hub_root = "/home/me/resalloc"
```

On the hub machine itself `ssh_host` is empty: the daemon only looks at its own disk.

`resalloc hub install` prepares the directory on this machine. `resalloc hub bootstrap` copies the package to the SSH host, installs the `~/.local/bin/resalloc` wrapper, and enables a user systemd service when that account has one. Secrets: `<hub_root>/secrets.env`, mode `600`. Modal and Kaggle are installed on the hub only when their keys are already in that file, or with `--extras all`.

The daemon loop accepts `inbox/`, starts a provider, polls the budget, asks for a checkpoint at the soft threshold, and cuts the run at the hard threshold even without an answer. `confirm=True` moves the job to `awaiting_confirm` and does not keep a paid GPU. Commands from the laptop are files in `jobs/<id>/commands/`: `confirm`, `edit`, `resume`, `data_ready`.

How many jobs may run is `hub.toml` in the hub root. Environment variables override the file.

```toml
max_running = 8
tick_workers = 4
probe_failures = 5

[providers.runpod]
max_running = 2
```

The top-level `max_running` is the cap for every job. A provider section is that provider's own cap. A job that does not fit stays `queued` with reason `at_capacity`. `RESALLOC_TICK_WORKERS` probes several live jobs in parallel. Starts themselves run one at a time. `probe_failures` is how many ticks an API may fail before the hub treats the workload as gone and releases it. The systemd unit sets `RESALLOC_TICK_WORKERS=4`. A new `hub.toml` includes these lines as comments, next to `reserve_bytes`. A file that already exists is left as it is.

When a job enters `awaiting_confirm`, the hub sends one notice. Later ticks and a daemon restart do not send it again.

```toml
notify_url = "https://ntfy.sh/your-topic"
notify_command = "notify-send resalloc"
```

`notify_url` gets a POST of `{job_id, state, reason, provider}`. The address has to be `http` or `https`. `notify_command` runs with no shell: `shlex` splits the arguments. The command's environment includes `RESALLOC_JOB_ID`, `RESALLOC_NOTIFY_STATE`, `RESALLOC_NOTIFY_REASON`, and `RESALLOC_NOTIFY_PROVIDER`. `RESALLOC_NOTIFY_URL` and `RESALLOC_NOTIFY_COMMAND` override the file. `resalloc config` only says whether a notice is configured, and does not print the address.

A job id is letters, digits, dot, `_`, and `-` only. A dataset hash is sha256. A `script` path or a dataset name that leaves the job directory is refused.

`data_endpoint` in the journal is where the laptop sends a dataset past the hub disk (pod SSH or a volume name). The adapter writes it into `RunningJob.connection`.

How to turn it on:

1. `resalloc up` when the hub is this machine. When this laptop only submits jobs and the hub is hosting: `resalloc up --ssh-host NAME`. `NAME` must already be a `Host` in `~/.ssh/config`. The directory on that host defaults to `~/resalloc`. `bootstrap` by itself does not create a virtualenv and does not download Modal or Kaggle unless you pass `--extras all`.
2. Keys for the providers you use go in `secrets.env` on the hub. Runpod, Vast, Modal, and Kaggle do not start without theirs. The laptop and an SSH machine start with no keys.
3. After editing secrets, restart the daemon (`systemctl --user restart resalloc-hub`, or `resalloc hub run` again).
4. A direct upload to Modal or Kaggle also needs those keys on the laptop: the bytes skip the hub.
5. `resalloc hub bootstrap --extras all` installs the `modal` and `kaggle` packages on the hub (plain `bootstrap` does it too once the keys are already in the file).
6. `resalloc doctor` shows what is missing. It does not print secret values.
