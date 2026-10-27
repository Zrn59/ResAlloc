# Kaggle

[English](../en/providers/kaggle.md) · **Русский**

Код: `src/resalloc/providers/kaggle.py`, прямая заливка в `src/resalloc/data_stores.py`.

Официальный CLI: `kaggle quota --csv`, `kernels push/status/output`, `datasets create/version`.

Скриптовое ядро получает в `/kaggle/src` только файл запуска. Код задачи и пакет `resalloc` заливаются отдельным приватным датасетом и монтируются в `/kaggle/input` до старта GPU. Текущий CLI пропускает каталоги, поэтому они уходят как tar, а ядро распаковывает их само. Push ядра ждёт статус датасета `ready`: иначе Kaggle молча выкидывает источник и ядро стартует без файлов.

| Настройка | Куда попадает |
| --- | --- |
| `accelerator` | `machine_shape`. Имя с `Tpu` включает TPU, иначе GPU |
| `internet` | `enable_internet` |
| `title` | заголовок ядра |
| `private` | `is_private`, по умолчанию да |
| `kernel_type` | `script` или `notebook` |
| `dataset_slug` | готовый датасет в `dataset_sources`, без новой заливки с хаба |

Квота читается при старте. Одна сессия по документации Kaggle — 12 часов для GPU и CPU и 9 часов для TPU; аренда короче на час, чтобы чекпоинт успел записаться в `/kaggle/working` до того, как платформа убьёт ядро. Недельный остаток сам по себе этого не ограничивает. Выход ядра, который можно скачать, — только `/kaggle/working`. Часть версий CLI падает на длительности `0s`. Это баг Kaggle.

`route="direct"` или `stream` гоняет `kaggle datasets` по исходной папке на ноутбуке. Второго экземпляра на хабе нет. Пофайлового потока в уже запущенное ядро у Kaggle нет: датасет монтируется к старту. `stream` здесь значит «не копировать дерево во временную папку», а не «читать по мере прибытия».

Текущий CLI берёт `KAGGLE_API_TOKEN` или файл `~/.kaggle/access_token`. Старые `KAGGLE_KEY` и `kaggle.json` он не использует, поэтому токен в кабинете Kaggle может числиться как Never used, пока строка `KAGGLE_API_TOKEN` не появится в `secrets.env` на хабе и демон не будет перезапущен.
