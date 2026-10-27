# Runpod

[English](../en/providers/runpod.md) · **Русский**

Код: `src/resalloc/providers/runpod.py`.

REST v2, база `https://api.runpod.io`. Ключ `RUNPOD_API_KEY` на хабе. GraphQL не используется.

| Настройка | Куда попадает |
| --- | --- |
| `gpu`, `gpu_count` | `gpu.id`, `gpu.count` |
| `spot` | облако `COMMUNITY`, если `cloud` не задан |
| `cloud` | `SECURE` или `COMMUNITY` как есть |
| `image` | образ, иначе официальный PyTorch |
| `disk_gb` | диск контейнера |
| `volume_gb` | постоянный диск `/data` |
| `network_volume_id` | сетевой том на `/data` |
| `data_centers` | `dataCenterIds` |
| `ports` | плюс `22/tcp`, он нужен для SSH |
| `env` | переменные контейнера |

Создание: `POST /v2/pods`. `entrypoint` и `cmd` не передаются: у `runpod/pytorch` их замена выкидывает `/start.sh`, а именно он пишет `PUBLIC_KEY` в `authorized_keys` и запускает `sshd`. Этот скрипт сам заканчивается на `sleep infinity`, под не гаснет. В `env.PUBLIC_KEY` кладётся публичный ключ хаба (`~/.ssh/id_ed25519.pub`), плюс `startSsh`. После `RUNNING` код ждёт, пока прямой SSH ответит на `echo ok` (порт в API появляется раньше, чем демон), и только потом льёт файлы. Скрипт идёт в фоне и по выходу пишет `finished.json`. Иначе под остаётся `RUNNING` и продолжает стоить деньги. Копирование файлов идёт по `ssh.direct`: прокси `ssh.runpod.io` умеет только шелл и не принимает rsync. Если прямого адреса нет, и заливка, и скачивание чекпоинта идут tar-потоком через шелл. Сигнал остановки тоже. Остановка после чекпоинта — `DELETE`.

Деньги: `GET /v2/billing` и цена из `GET /v2/catalog/gpus`. Живого баланса в опубликованной v2 нет. Если account-эндпоинт всё же вернёт `balance`, `clientBalance` или `creditBalance`, он используется вместе с `runpod_min_balance_usd`. Иначе потолок — `runpod_max_spend_usd`. Расход — большее из прироста биллинга и `цена × время`.

Датасет `keep="runpod"`: ноутбук получает `data_endpoint.ssh` и льёт файлы на под сам. `route="direct"` держит скрипт, пока не появится `COMPLETE`. `route="stream"` пускает скрипт сразу.
