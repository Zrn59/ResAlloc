# Хаб

[English](en/hub.md) · **Русский**

Код: `src/resalloc/hub.py`, `src/resalloc/transport.py`, `src/resalloc/setup.py`, `src/resalloc/config.py`.

Конфиг — `~/.config/resalloc/config.toml`. Его пишет `resalloc up` или `resalloc init`. Без `ssh_host` хаб — эта же машина, каталог по умолчанию `~/resalloc`. С `ssh_host` это имя `Host` из `~/.ssh/config`: пользователь, порт и ключ живут там, не в библиотеке.

```toml
ssh_host = "my-server"
hub_root = "/home/me/resalloc"

[limits]
modal_max_spend_usd = 10
runpod_max_spend_usd = 5
vast_max_spend_usd = 5
```

Имена в `[limits]` те же, что у `Limits.to_dict()`: буферы задаются секундами (`soft_buffer_seconds`, `hard_buffer_seconds`). Поле, которое в `Job` осталось на значении по умолчанию, берётся отсюда. Значение, заданное в задаче, остаётся. `resalloc up` пишет файл, если его ещё нет. `resalloc config` показывает эту сторону и сторону хаба и не печатает значения секретов.

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

`max_running` сверху — потолок на все задачи. Секция провайдера — его собственный потолок. Не влезшая задача остаётся `queued` с причиной `at_capacity`. `RESALLOC_TICK_WORKERS` опрашивает несколько живых задач параллельно; сами старты идут по одному. `probe_failures` — сколько тиков API может молчать, прежде чем хаб посчитает запуск пропавшим и снимет его. Юнит systemd ставит `RESALLOC_TICK_WORKERS=4`. Новый `hub.toml` содержит эти строки закомментированными, рядом с `reserve_bytes`. Уже записанный файл не переписывается.

Когда задача переходит в `awaiting_confirm`, хаб один раз шлёт уведомление. Повторные тики и перезапуск демона его не повторяют.

```toml
notify_url = "https://ntfy.sh/your-topic"
notify_command = "notify-send resalloc"
```

`notify_url` получает POST с JSON `{job_id, state, reason, provider}`. Адрес должен быть `http` или `https`. `notify_command` запускается без оболочки: аргументы разделяет `shlex`. В окружении команды есть `RESALLOC_JOB_ID`, `RESALLOC_NOTIFY_STATE`, `RESALLOC_NOTIFY_REASON` и `RESALLOC_NOTIFY_PROVIDER`. `RESALLOC_NOTIFY_URL` и `RESALLOC_NOTIFY_COMMAND` перекрывают файл. `resalloc config` показывает только, включено уведомление или нет, и не печатает адрес.

Идентификатор задачи — только буквы, цифры, точка, `_` и `-`. Хеш датасета — sha256. Чужие пути в `script` и в имени датасета хаб не принимает.

`data_endpoint` в журнале — куда ноутбуку лить датасет мимо диска хаба (SSH пода или имя тома). Его пишет адаптер в `RunningJob.connection`.

Как включить:

1. `resalloc up`, если хаб на этой машине. Если хаб на хостинге, а этот ноутбук только сдаёт задачи: `resalloc up --ssh-host ИМЯ`. Имя уже должно быть `Host` в `~/.ssh/config`. Каталог на хосте по умолчанию `~/resalloc`. `bootstrap` сам по себе не ставит venv и не качает Modal или Kaggle, пока не передан `--extras all`.
2. Ключи нужных провайдеров — в `secrets.env` на хабе. Runpod, Vast, Modal и Kaggle без своих ключей не стартуют. Ноутбук и SSH-машина стартуют без ключей.
3. После правки секретов перезапусти демон (`systemctl --user restart resalloc-hub`, либо снова `resalloc hub run`).
4. Для прямой заливки датасета на Modal или Kaggle те же ключи нужны и на ноутбуке: байты идут мимо хаба.
5. Пакеты `modal` и `kaggle` на хаб ставит `resalloc hub bootstrap --extras all` (или сам bootstrap, если ключи уже записаны).
6. `resalloc doctor` показывает, чего не хватает. Секреты он не печатает.

Проверка: `resalloc doctor`.
