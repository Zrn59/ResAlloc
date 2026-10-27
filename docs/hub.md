# Хаб

[English](en/hub.md) · **Русский**

Код: `src/resalloc/hub.py`, `src/resalloc/transport.py`, `src/resalloc/setup.py`, `src/resalloc/config.py`.

Конфиг — `~/.config/resalloc/config.toml`. Его пишет `resalloc init`. Без `ssh_host` хаб — эта же машина, каталог по умолчанию `~/resalloc`. С `ssh_host` это имя `Host` из `~/.ssh/config`: пользователь, порт и ключ живут там, не в библиотеке.

```toml
ssh_host = "my-server"
hub_root = "/home/me/resalloc"
```

На самой машине хаба `ssh_host` пустой: демон смотрит только свой диск.

`resalloc hub install` готовит каталог на этой машине. `resalloc hub bootstrap` копирует пакет на SSH-хост, ставит обёртку `~/.local/bin/resalloc` и включает user-systemd, если он там есть. Секреты: `<hub_root>/secrets.env`, права `600`. Modal и Kaggle ставятся на хаб, только когда их ключи уже лежат в этом файле, либо с `--extras all`.

Цикл демона: забрать `inbox/`, стартануть провайдера, опрашивать бюджет, на мягком пороге попросить чекпоинт, на жёстком погасить даже без ответа. `confirm=True` переводит в `awaiting_confirm` и не держит платный GPU. Команды с ноутбука — файлы в `jobs/<id>/commands/`: `confirm`, `edit`, `resume`, `data_ready`.

Сколько задач пускать сразу, пишется в `hub.toml` в корне хаба. Переменные окружения перекрывают файл.

```toml
max_running = 8
tick_workers = 4
probe_failures = 5

[providers.runpod]
max_running = 2
```

`max_running` сверху — потолок на все задачи. Секция провайдера — его собственный потолок. Не влезшая задача остаётся `queued` с причиной `at_capacity`. `RESALLOC_TICK_WORKERS` опрашивает несколько живых задач параллельно; сами старты идут по одному. `probe_failures` — сколько тиков API может молчать, прежде чем хаб посчитает запуск пропавшим и снимет его. Юнит systemd ставит `RESALLOC_TICK_WORKERS=4`.

Идентификатор задачи — только буквы, цифры, точка, `_` и `-`. Хеш датасета — sha256. Чужие пути в `script` и в имени датасета хаб не принимает.

`data_endpoint` в журнале — куда ноутбуку лить датасет мимо диска VM (SSH пода или имя тома). Его пишет адаптер в `RunningJob.connection`.

Как включить:

1. `resalloc init --local` и `resalloc hub install`, если хаб на этой машине. Либо `resalloc init --ssh-host ИМЯ --hub-root КАТАЛОГ` и `resalloc hub bootstrap`, если хаб на другой.
2. Ключи нужных провайдеров — в `secrets.env` на хабе. Runpod, Vast, Modal и Kaggle без своих ключей не стартуют. Ноутбук и SSH-машина стартуют без ключей.
3. После правки секретов перезапусти демон (`systemctl --user restart resalloc-hub`, либо снова `resalloc hub run`).
4. Для прямой заливки датасета на Modal или Kaggle те же ключи нужны и на ноутбуке: байты идут мимо хаба.
5. Пакеты `modal` и `kaggle` на хаб ставит `resalloc hub bootstrap --extras all` (или сам bootstrap, если ключи уже записаны).
6. `resalloc doctor` показывает, чего не хватает. Секреты он не печатает.

Проверка: `resalloc doctor`.
