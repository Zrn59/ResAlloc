# Команды

[English](en/cli.md) · **Русский**

Код: `src/resalloc/cli.py`.

| Команда | Зачем |
| --- | --- |
| `resalloc` / `resalloc help` | короткая настройка: хаб здесь или ноутбук плюс хостинг-узел |
| `resalloc config` | ноутбук, каталог хаба, резерв диска, потолки, имена секретов. Значения ключей не печатает |
| `resalloc up` | хаб на этой машине. `--ssh-host ИМЯ` ставит узел на хост из `~/.ssh/config`. `--no-start` только для локального хаба |
| `resalloc init` | записать `~/.config/resalloc/config.toml`. Флаги `--local`, `--ssh-host`, `--hub-root`. Уже существующий файл не перезаписывается без `--force` |
| `resalloc submit job.json` | сдать заявку. В JSON те же поля, что у `Job` |
| `resalloc status [JOB]` | состояние, провайдер, шаг, свежесть чекпоинта, следующий шаг |
| `resalloc attach JOB` | ждать и ответить `confirm` или `edit gpu=..., spot=true` |
| `resalloc edit JOB --gpu ...` | поменять следующий провайдер, пока задача на паузе. Ещё `--volume`, `--region`, `--cloud`, `--max-price`, `--host` |
| `resalloc confirm JOB` | продолжить после паузы |
| `resalloc resume JOB` | снова запустить остановленную задачу с чекпоинта |
| `resalloc doctor` | SSH, диск, пакет, демон, linger, секреты и отдельно `KAGGLE_API_TOKEN` |
| `resalloc test modal kaggle` | минутный PyTorch на выбранных площадках: `modal`, `kaggle`, `runpod`, `vast`, `laptop`. Modal ограничен $0.10, Runpod и Vast — $0.20. `laptop` сам поднимает агент, если пульса ещё нет. Vast не стартует без `VAST_API_KEY` в `secrets.env` |
| `resalloc hub install` | подготовить каталог хаба на этой машине. Отказывается, если конфиг указывает на SSH-хост |
| `resalloc hub bootstrap` | поставить и запустить хаб на `ssh_host`. `--extras all` дополнительно ставит Modal и Kaggle |
| `resalloc hub run` | цикл демона на этой машине |
| `resalloc laptop agent` | ноутбук как бесплатный провайдер |
| `resalloc laptop push JOB` | дослать чекпоинт с ноутбука сразу после `save` |

`JOB` — имя каталога, которое печатает `resalloc submit`, например `train-2026-10-31-18-11-a3f9`. `resalloc config --json` печатает те же настройки одним объектом для скрипта. Значений ключей там нет.

`edit` в `attach` понимает `key=value` через запятую. `spot` и `internet` — булевы. `disk_gb`, `cpus`, `gpu_count`, `memory_mb`, `ram_gb`, `timeout_hours`, `cpu`, `max_price` — числа. Имя GPU остаётся строкой. `volume`, `region`, `cloud` и `host` тоже строки. `--json` у `edit` — это объект с полями, а не переключатель. Флаги дополняют его.
