# Changelog

[English](#english) · [Русский](#русский)

## English

### 0.2.4

- Job folders are named from the script and the local time: `jobs/train-2026-10-31-18-11-a3f9`. `name="city"` replaces the script stem. The same name is the Runpod pod, the Modal volume, the Kaggle kernel, and the SSH directory.
- Inside the folder, `checkpoints` is the intermediate state and `logs` is the output.
- Install from GitHub: `pip install git+https://github.com/Zrn59/ResAlloc.git`. A release tag is `git+https://github.com/Zrn59/ResAlloc.git@v0.2.4`.

### 0.2.3

- `resalloc config` shows the laptop, the hub, the disk reserve, the caps, and secret names. It does not print secret values or the notify address.
- `[limits]` in `config.toml` fills spend caps a job left at their defaults. A value set on the job wins.
- `resalloc edit` accepts `--volume`, `--region`, `--cloud`, `--max-price`, and `--host`.
- A new `hub.toml` includes commented caps next to `reserve_bytes`. An existing file is left as it is.
- When a job enters `awaiting_confirm`, the hub sends one notice: POST JSON to `notify_url`, or run `notify_command` with no shell. Later ticks do not send it again.

### 0.2.2

- `resalloc help` prints the short setup. `resalloc` with no arguments does the same.
- `resalloc up --ssh-host NAME` installs the hub on a `Host` from `~/.ssh/config`.

### 0.2.1

- `resalloc up` prepares a local hub in one step.
- Install does not create a virtualenv and does not download Modal or Kaggle unless `--extras` asks for them.
- A smaller disk gets a smaller free-space reserve: 1 GiB when at least 8 GiB is free, 20 GiB when at least 80 GiB is free.
- A streamed dataset is scanned again after its `COMPLETE` marker appears.

### 0.2.0

First public release. One Python job moves across Runpod, Vast.ai, Modal, Kaggle, an SSH machine, and the laptop. The hub keeps checkpoints and stops or moves the job before a spend or quota limit.

## Русский

### 0.2.4

- Каталог задачи называется по скрипту и местному времени: `jobs/train-2026-10-31-18-11-a3f9`. `name="city"` подставляет своё имя. То же имя у пода Runpod, тома Modal, ядра Kaggle и каталога на SSH.
- Внутри `checkpoints` — промежуточное, `logs` — вывод.
- Установка с GitHub: `pip install git+https://github.com/Zrn59/ResAlloc.git`. Релизный тег: `git+https://github.com/Zrn59/ResAlloc.git@v0.2.4`.

### 0.2.3

- `resalloc config` показывает ноутбук, хаб, резерв диска, потолки и имена секретов. Значения ключей и адрес уведомления не печатает.
- `[limits]` в `config.toml` заполняет потолки, которые задача оставила по умолчанию. Значение, заданное в задаче, остаётся.
- `resalloc edit` принимает `--volume`, `--region`, `--cloud`, `--max-price` и `--host`.
- Новый `hub.toml` содержит закомментированные потолки рядом с `reserve_bytes`. Уже записанный файл не переписывается.
- Когда задача переходит в `awaiting_confirm`, хаб один раз шлёт уведомление: POST JSON на `notify_url` или команда `notify_command` без оболочки. Следующие тики его не повторяют.

### 0.2.2

- `resalloc help` печатает короткую настройку. `resalloc` без аргументов делает то же самое.
- `resalloc up --ssh-host ИМЯ` ставит хаб на `Host` из `~/.ssh/config`.

### 0.2.1

- `resalloc up` готовит локальный хаб одной командой.
- Установка не создаёт venv и не скачивает Modal или Kaggle, пока это не попросили через `--extras`.
- На небольшом диске резерв меньше: 1 ГиБ, если свободно хотя бы 8 ГиБ, и 20 ГиБ, если свободно хотя бы 80 ГиБ.
- Потоковый датасет сканируется ещё раз после маркера `COMPLETE`.

### 0.2.0

Первый публичный релиз. Один Python-скрипт переносится между Runpod, Vast.ai, Modal, Kaggle, SSH-машиной и ноутбуком. Хаб хранит чекпоинты и останавливает или переносит задачу до лимита денег или квоты.
