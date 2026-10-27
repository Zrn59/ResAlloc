# Команды

[English](en/cli.md) · **Русский**

Код: `src/resalloc/cli.py`.

| Команда | Зачем |
| --- | --- |
| `resalloc init` | записать `~/.config/resalloc/config.toml`. Флаги `--local`, `--ssh-host`, `--hub-root`. Уже существующий файл не перезаписывается без `--force` |
| `resalloc submit job.json` | сдать заявку. В JSON те же поля, что у `Job` |
| `resalloc status [JOB]` | состояние, провайдер, шаг, свежесть чекпоинта, следующий шаг |
| `resalloc attach JOB` | ждать и ответить `confirm` или `edit gpu=..., spot=true` |
| `resalloc edit JOB --gpu ...` | поменять настройки следующего провайдера, пока задача на паузе |
| `resalloc confirm JOB` | продолжить после паузы |
| `resalloc resume JOB` | снова запустить остановленную задачу с чекпоинта |
| `resalloc doctor` | SSH, диск, пакет, демон, linger, секреты и отдельно `KAGGLE_API_TOKEN` |
| `resalloc test modal kaggle` | минутный PyTorch на выбранных площадках: `modal`, `kaggle`, `runpod`, `vast`, `laptop`. Modal ограничен $0.10, Runpod и Vast — $0.20. `laptop` сам поднимает агент, если пульса ещё нет. Vast не стартует без `VAST_API_KEY` в `secrets.env` |
| `resalloc hub install` | подготовить каталог хаба на этой машине. Отказывается, если конфиг указывает на SSH-хост |
| `resalloc hub bootstrap` | поставить и запустить хаб на `ssh_host`. `--extras all` дополнительно ставит Modal и Kaggle |
| `resalloc hub run` | цикл демона на этой машине |
| `resalloc laptop agent` | ноутбук как бесплатный провайдер |
| `resalloc laptop push JOB` | дослать чекпоинт с ноутбука сразу после `save` |

`edit` в `attach` понимает `key=value` через запятую. `spot` и `internet` — булевы. `disk_gb`, `cpus`, `gpu_count`, `memory_mb`, `ram_gb`, `timeout_hours`, `cpu` — числа. Имя GPU остаётся строкой.
