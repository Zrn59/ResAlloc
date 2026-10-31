# Какую доку открыть

[English](en/README.md) · **Русский**

Коротко: [SHORT.md](SHORT.md). Главные страницы: [README.ru.md](../README.ru.md), [README.md](../README.md). Изменения по версиям: [CHANGELOG.md](../CHANGELOG.md).

С чего начать: `pip install git+https://github.com/Zrn59/ResAlloc.git`, затем `resalloc help`. Хаб на этой машине — `resalloc up`. Этот ноутбук только сдаёт задачи, а хаб на хостинге — `resalloc up --ssh-host ИМЯ`, где `ИМЯ` уже есть в `~/.ssh/config`. Дальше ключи в `secrets.env` на хабе и `resalloc doctor`. Текущие настройки без значений ключей: `resalloc config`.

| Вопрос | Файл |
| --- | --- |
| Где какой код | [MAP.md](MAP.md) |
| Задача, лимиты, Worker | [job.md](job.md) |
| Куда деваются байты: hub, direct, stream | [data.md](data.md) |
| Хаб, SSH, демон, подтверждение | [hub.md](hub.md) |
| Команды | [cli.md](cli.md) |
| Runpod | [providers/runpod.md](providers/runpod.md) |
| Vast.ai | [providers/vast.md](providers/vast.md) |
| SSH-машина | [providers/machine.md](providers/machine.md) |
| Modal | [providers/modal.md](providers/modal.md) |
| Kaggle | [providers/kaggle.md](providers/kaggle.md) |
| Ноутбук | [providers/laptop.md](providers/laptop.md) |
