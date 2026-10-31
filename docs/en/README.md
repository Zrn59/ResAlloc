# Which doc to open

**English** · [Русский](../README.md)

Short version: [SHORT.md](SHORT.md). Front pages: [README.md](../../README.md), [README.ru.md](../../README.ru.md). Version history: [CHANGELOG.md](../../CHANGELOG.md).

Start with `pip install git+https://github.com/Zrn59/ResAlloc.git`, then `resalloc help`. This machine as the hub is `resalloc up`. This laptop only submits jobs and the hub is another host: `resalloc up --ssh-host NAME`, where `NAME` is already a `Host` in `~/.ssh/config`. Then put keys in `secrets.env` on the hub and run `resalloc doctor`. Current settings, without secret values: `resalloc config`.

| Question | File |
| --- | --- |
| Where the code lives | [MAP.md](MAP.md) |
| Job, limits, Worker | [job.md](job.md) |
| Where the bytes go: hub, direct, stream | [data.md](data.md) |
| Hub, SSH, daemon, confirmation | [hub.md](hub.md) |
| Commands | [cli.md](cli.md) |
| Runpod | [providers/runpod.md](providers/runpod.md) |
| Vast.ai | [providers/vast.md](providers/vast.md) |
| SSH machine | [providers/machine.md](providers/machine.md) |
| Modal | [providers/modal.md](providers/modal.md) |
| Kaggle | [providers/kaggle.md](providers/kaggle.md) |
| Laptop | [providers/laptop.md](providers/laptop.md) |
