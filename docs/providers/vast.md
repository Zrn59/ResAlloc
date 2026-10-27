# Vast.ai

[English](../en/providers/vast.md) · **Русский**

Код: `src/resalloc/providers/vast.py`.

REST `https://console.vast.ai/api/v0`. Ключ `VAST_API_KEY` на хабе. Публичный SSH-ключ этого же аккаунта нужно зарегистрировать в Vast до первого запуска: API не подкладывает ключ в инстанс. Приватный ключ хаба — `VAST_SSH_IDENTITY` или `RESALLOC_SSH_IDENTITY`. Для прямой заливки датасета тот же ключ должен открывать SSH и с ноутбука.

| Настройка | Куда попадает |
| --- | --- |
| `gpu` | `gpu_name`. `NVIDIA GeForce RTX 4090` превращается в `RTX 4090` |
| `gpu_count` | `num_gpus` |
| `spot` | поиск `type=bid`, в создание уходит `price` выбранного оффера |
| `image` | образ, иначе `pytorch/pytorch:2.4.0-cuda12.4-cudnn9-runtime` |
| `disk_gb` | диск инстанса |
| `max_price` | потолок `$/час`, иначе `vast_max_hourly_usd` |
| `min_reliability` | фильтр `reliability` |
| `require_direct_ports` | по умолчанию нужен хотя бы один прямой порт |
| `env` | переменные контейнера |

Поиск: `POST /api/v0/bundles/`. Создание: `PUT /api/v0/asks/{id}/` с `runtype=ssh` и `cancel_unavail`. Ответный `new_contract` — id инстанса. Пока `actual_status` не `running` и нет `ssh_host`/`ssh_port`, хаб ждёт. `exited`, `unknown` и `offline` сами до `running` не доходят: инстанс удаляется и провайдер пропускается. Остановка — `DELETE /api/v0/instances/{id}/`. Простой stop продолжает брать деньги за диск.

Деньги: `GET /api/v0/users/current` (`balance`) и `dph_total`. Потолок сессии — `vast_max_spend_usd`, пол — `vast_min_balance_usd`. Расход — большее из падения баланса и `цена × время`. Баланс кэшируется на 30 секунд, чтобы пачка задач не упиралась в лимит запросов. `429` на чтении и поиске повторяется. Создание инстанса не повторяется: повтор мог бы снять второй оффер.

Скрипт и чекпоинт ходят по SSH так же, как на Runpod: каталог `/resalloc`, выход пишется в `finished.json`, после этого инстанс удаляется.
