# Задача и скрипт

[English](en/job.md) · **Русский**

Код: `src/resalloc/models.py`, `src/resalloc/worker.py`, `src/resalloc/policy.py`.

`Job` описывает скрипт, стартовый провайдер, `failover` и датасеты. Строка в `datasets` значит `route="hub"`. Объект `Dataset` выбирает дорогу.

Каталог задачи читается сразу: `jobs/train-2026-10-31-18-11-a3f9`. Имя берётся из файла скрипта, время — местное в момент сдачи, четыре знака в конце отличают две сдачи в одну минуту. Своё имя: `Job(..., name="city")` или поле `name` в JSON. То же имя уходит в под Runpod, том Modal, ядро Kaggle и каталог на SSH-машине. Это же имя печатает `resalloc submit` и его ждут `status`, `confirm`, `edit`, `resume`.

```text
jobs/train-2026-10-31-18-11-a3f9/
  checkpoints/    промежуточное: state.json и artifacts, их подхватит следующий провайдер
  logs/           вывод, снятый с GPU: журналы и превью
  runtime/        живые файлы, пока задача идёт
  datasets/       датасеты с дорогой hub
  bundle/         скрипт и requirements
  journal.json    состояние, причина, провайдер
```

`Limits` решает, когда просить сохраниться (`soft_buffer`) и когда гасить запуск даже без ответа (`hard_buffer`).

| Поле | Смысл |
| --- | --- |
| `runpod_min_balance_usd` | не спускать баланс ниже этого, если API его отдал |
| `runpod_max_spend_usd` | потолок сессии, если живого баланса нет |
| `runpod_max_hourly_usd` | не брать GPU дороже |
| `modal_max_spend_usd` | потолок по `metered_cost`, до вычета кредитов, не «остаток от $30» |
| `kaggle_min_gpu_hours` | оставить запас часов GPU/TPU |
| `vast_min_balance_usd` | не спускать баланс Vast ниже этого |
| `vast_max_spend_usd` | потолок сессии на Vast |
| `vast_max_hourly_usd` | не брать оффер дороже |
| `laptop_heartbeat_timeout` | через сколько молчания ноут считается упавшим |

Те же имена, что у `Limits.to_dict()`, можно записать один раз в `~/.config/resalloc/config.toml`, секция `[limits]`: `soft_buffer_seconds`, `modal_max_spend_usd`, `runpod_max_spend_usd` и остальные. При `submit` поле, оставленное на значении по умолчанию, берётся оттуда. Значение, заданное в самом `Job`, остаётся. `data_timeout_seconds` в эту секцию не входит.

`data_timeout_seconds` лежит на `Job`, не в `Limits`: сколько ждать SSH, чтобы дослать прямой датасет. Потом скрипт сам перестаёт ждать и пишет ненулевой `finished.json`, чтобы GPU не висел без данных.

Ненулевой выход скрипта — `STOP` с причиной `worker_failed`. Обрыв пода, потеря пульса ноутбука и несколько неудачных `probe` подряд по-прежнему переносят задачу, если следующий шаг есть.

В `train.py` один и тот же код на всех провайдерах:

```python
worker = Worker()
state = worker.load()
worker.on_stop(save_weights)   # вызывается до ack
worker.save({"epoch": epoch})
signal = worker.pulse()        # continue / checkpoint_and_migrate / stop
for path in worker.iter_ready("huge"):
    ...
worker.wait_data("huge")       # пока не появится COMPLETE
```

`worker.path("model.pt")` — файл, который уедет вместе с чекпоинтом на хаб.

Решение политики: много запаса — `CONTINUE`; запас внутри мягкого порога — `MIGRATE` или `PARK`, если следующий шаг просит подтверждение; жёсткий порог или обрыв — без ожидания ack. Успешный конец скрипта — `STOP` с причиной `completed`, без переноса.
