# Карта проекта

[English](en/MAP.md) · **Русский**

```
src/resalloc/
  __init__.py          публичные имена: Job, Dataset, ResAlloc, Worker, провайдеры
  models.py            Job, Limits, Dataset, RunPod, Vast, Modal, Kaggle, Machine, Laptop
  client.py            сдача задачи с ноутбука
  data.py              куски, DirectorySink, выбор keep/store
  data_stores.py       прямая заливка в Modal, Kaggle, Runpod, Vast и по SSH
  worker.py            код, который импортирует train.py
  policy.py            CONTINUE / MIGRATE / PARK / STOP
  hub.py               демон: очередь, пороги, перенос, чекпоинт
  layout.py            каталоги на диске хаба
  names.py             безопасные id задач, имена датасетов и хеши
  records.py           journal.json и команды confirm/edit/resume/data_ready
  transport.py         локальный диск или SSH+rsync
  config.py            ~/.config/resalloc/config.toml
  setup.py             doctor, init, install, bootstrap, attach
  laptop_agent.py      ноутбук сам стучится на хаб
  cli.py               команда resalloc
  providers/
    base.py            протокол адаптера и RunningJob
    registry.py        register_provider
    runpod.py          REST v2
    vast.py            REST Vast.ai и SSH
    machine.py         SSH на уже существующий хост
    modal_backend.py   Sandbox + Volume + billing
    kaggle.py          quota, kernels, datasets
    laptop.py          пульс и локальный процесс
deploy/resalloc-hub.service
docs/                  русские заметки
docs/en/               те же заметки по-английски
tests/                 без ключей и без живой VM
```

Поток управления всегда такой: ноутбук кладёт маленькую заявку на хаб, хаб вызывает API и хранит журнал. Поток байт датасета выбирается отдельно и может миновать хаб. Чекпоинт после остановки всё равно снимается на хаб.

Диск хаба: `inbox/`, `jobs/train-2026-10-31-18-11-a3f9/` (`bundle`, `runtime`, `checkpoints`, `logs`, `journal.json`), `cache/datasets/<hash>/`, `agents/laptop.json`. `checkpoints` — промежуточное, `logs` — вывод.
