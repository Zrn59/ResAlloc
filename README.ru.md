# resalloc

<div align="center">

[English](README.md) · **Русский** · [Docs](docs/en/README.md) · [Документация](docs/README.md)

<br>

[![zrn59.me](https://img.shields.io/badge/⚡_Powered_by-zrn59.me-f59e0b?style=for-the-badge&labelColor=18181b)](https://zrn59.me)
[![Donate](https://img.shields.io/badge/☕_Support_/_Donate-ff5f57?style=for-the-badge&labelColor=18181b)](https://www.donationalerts.com/r/zrn59)

*Custom Bot & Web Development • Data Parsing • Isolated Docker Hosting — **[zrn59.me](https://zrn59.me)***

</div>

---

Один обучающий скрипт и несколько GPU-провайдеров. Небольшой хаб, который работает постоянно, следит за деньгами и квотой, просит скрипт сохраниться и переносит задачу до лимита. Сам хаб модель не учит. Чекпоинты и журнал остаются на хабе. Большой датасет может идти сразу к провайдеру и не занимать диск хаба.

Встроенные провайдеры:

| Провайдер | Как хаб до него доходит | Чем ограничен |
| --- | --- | --- |
| Runpod | REST v2, затем SSH | баланс, если API его отдал, иначе `runpod_max_spend_usd` |
| Vast.ai | REST, затем SSH | баланс аккаунта и `vast_max_spend_usd` |
| Modal | Sandbox и Volume | `modal_max_spend_usd` по `metered_cost`, до вычета кредитов |
| Kaggle | официальный CLI | часы ускорителя; срок аренды запекается при push |
| Machine | SSH на уже имеющийся хост | бесплатно; остановка задачи машину не выключает |
| Laptop | ноутбук сам стучится на хаб | бесплатно, пока есть пульс |

Любой другой провайдер — объект и `register_provider`. Модуль из `RESALLOC_PROVIDER_MODULE` хаб импортирует при старте.

## Установка

Python 3.11 или новее, Linux или macOS. Хаб на другой машине дополнительно требует OpenSSH и rsync. Никакой конкретный хост, пользователь или облачный аккаунт не зашиты.

```bash
pip install resalloc
resalloc help
```

Два варианта:

```bash
# хаб на этой машине
resalloc up

# этот ноутбук сдаёт задачи, ИМЯ из ~/.ssh/config — узел-хаб
resalloc up --ssh-host ИМЯ
```

`resalloc up` пишет `~/.config/resalloc/config.toml`. Локально каталог хаба — `~/resalloc`. С `--ssh-host` каталог `~/resalloc` создаётся на том хосте, пакет копируется туда, и демон запускается, если у той учётки есть systemd. Больше ничего не скачивается. Modal и Kaggle, если они нужны: `resalloc up --ssh-host ИМЯ --extras all`.

Дальше ключи провайдеров в `secrets.env` на хабе и `resalloc doctor`. `resalloc config` показывает ноутбук, хаб, резерв диска, потолки и имена секретов. Значения ключей он не печатает. Общие потолки трат пишутся в `[limits]` в `config.toml`; значение, заданное в самой задаче, важнее. Все команды: `resalloc --help`. Короткие заметки: [docs/SHORT.md](docs/SHORT.md) и [docs/en/SHORT.md](docs/en/SHORT.md).

У ядра нет зависимостей. Задача только на ноутбуке или на своей SSH-машине не требует ключей API. На небольшом диске резерв — 1 ГиБ, на большом остаётся 20 ГиБ. Runpod и Vast работают на стандартной библиотеке. Modal и Kaggle ставятся через `pip install "resalloc[all]"` на той машине, которая вызывает их API.

Ключи остаются в `secrets.env` на хабе (`chmod 600`) и в репозиторий не попадают:

- `RUNPOD_API_KEY`
- `VAST_API_KEY` (публичный SSH-ключ аккаунта хаба нужно заранее зарегистрировать в Vast)
- токен Modal (`MODAL_TOKEN_ID` и `MODAL_TOKEN_SECRET`)
- `KAGGLE_USERNAME` и `KAGGLE_API_TOKEN`

## Задача

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

`confirm=True` не оставляет платный GPU включённым, пока ноутбук закрыт. Хаб снимает чекпоинт, отпускает машину и ждёт:

```bash
resalloc status
resalloc confirm JOB
resalloc resume JOB
```

`resalloc attach JOB` спрашивает в терминале. `resalloc submit job.json` принимает те же поля, что и `Job`.

Один и тот же `train.py` на всех провайдерах:

```python
from resalloc.worker import Worker

worker = Worker()
state = worker.load()

def flush():
    path = worker.path("model.pt")
    # записать веса в path

worker.on_stop(flush)
for epoch in range(state.get("epoch", 0), 10):
    worker.save({"epoch": epoch + 1})
    signal = worker.pulse()
    if signal.value != "continue":
        break
```

`pulse()` возвращает `continue`, `checkpoint_and_migrate` или `stop`. Тяжёлое сохранение клади в `on_stop`: оно вызывается до ack. Ненулевой выход — `worker_failed`. Хаб останавливает цепочку и не платит следующему провайдеру за тот же баг. После правки скрипта задачу продолжают через `resume`. Пропавший под, потерянный пульс ноутбука и молчащий API по-прежнему переносят задачу.

Ноутбук как счёт:

```bash
resalloc laptop agent
```

Агент сам стучится наружу. Хаб в NAT не подключается. Если стартовый провайдер — ноутбук, а пульса нет, сдача завершается ошибкой.

## Много задач

Один процесс хаба следит за всеми задачами. `RESALLOC_TICK_WORKERS` (по умолчанию 1, юнит systemd ставит 4) опрашивает несколько задач сразу. Старты идут по одному, чтобы два тика не сняли один и тот же слот дважды.

Потолки лежат в `hub.toml` на хабе. Переменные окружения перекрывают файл:

```toml
max_running = 8
tick_workers = 4
probe_failures = 5

[providers.runpod]
max_running = 2

[providers.vast]
max_running = 2
```

`max_running` сверху — сколько задач может идти всего. Секция провайдера — его собственный потолок. Задача, которой не хватило места, остаётся `queued` с причиной `at_capacity` и пробуется на следующем тике. После `probe_failures` неудачных опросов хаб считает запуск пропавшим, чтобы зависший API не оставлял GPU в оплате.

Диск хаба общий с системой. Резерв по умолчанию 20 ГиБ (`RESALLOC_RESERVE_BYTES`). Кэш датасетов вытесняется по давности, кроме хешей, которые заняты живой задачей. Новая заливка на водяном знаке не принимается. Уже идущая задача чекпоинт дописывает. Отклонённая заявка сохраняет бандл, поэтому `resalloc resume` можно повторить, когда место освободится.

## Данные

Повторный датасет узнаётся по хешу содержимого и второй раз не заливается.

- `hub` копирует его на хаб. Для маленьких наборов.
- `direct` отправляет файлы с машины, которая сдаёт задачу, сразу в хранилище провайдера.
- `stream` делает то же кусками по 8 МиБ и помечает каждый готовый файл, чтобы обучение могло читать его раньше.

```python
from resalloc import Dataset, Job, Modal, ResAlloc

ResAlloc.submit(Job(
    script="train.py",
    start=Modal(gpu="A10G", volume="datasets"),
    datasets=[Dataset("./huge", route="direct", keep="modal", store="datasets")],
))
```

Байты Modal и Kaggle заливаются до старта GPU. Runpod, Vast и SSH-машине сначала нужен SSH-адрес, поэтому заливка ждёт, пока хаб его опубликует. Если данные не доехали за `data_timeout_seconds` (по умолчанию 3600), скрипт перестаёт ждать, и хаб отпускает GPU.

## Лицензия

MIT. См. [LICENSE](LICENSE). Ключи API в репозиторий не кладут.
