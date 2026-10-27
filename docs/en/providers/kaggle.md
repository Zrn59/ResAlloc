# Kaggle

**English** · [Русский](../../providers/kaggle.md)

Code: `src/resalloc/providers/kaggle.py`. Direct upload: `src/resalloc/data_stores.py`.

The official CLI: `kaggle quota --csv`, `kernels push/status/output`, `datasets create/version`.

A script kernel receives only the launcher under `/kaggle/src`. The job code and the `resalloc` package are uploaded as a separate private dataset and mounted at `/kaggle/input` before the GPU starts. The current CLI skips directories, so they are sent as tar and the kernel unpacks them. The kernel push waits until the dataset status is `ready`. Otherwise Kaggle silently drops the source and the kernel starts without the files.

| Setting | Where it goes |
| --- | --- |
| `accelerator` | `machine_shape`. A name that starts with `Tpu` enables TPU, otherwise GPU |
| `internet` | `enable_internet` |
| `title` | kernel title |
| `private` | `is_private`, true by default |
| `kernel_type` | `script` or `notebook` |
| `dataset_slug` | an existing dataset in `dataset_sources`, with no new upload from the hub |

Quota is read at start. One Kaggle session is 12 hours for GPU and CPU and 9 hours for TPU. The lease is one hour shorter, so the checkpoint can land in `/kaggle/working` before the platform kills the kernel. The weekly remainder does not enforce that by itself. The only output that can be downloaded is `/kaggle/working`. Some CLI versions crash on a duration of `0s`. The hub parses that form. The crash itself is a Kaggle bug.

`route="direct"` or `stream` runs `kaggle datasets` against the original folder on the laptop. There is no second copy on the hub. Kaggle cannot stream file by file into a kernel that is already running: the dataset is mounted at start. Here `stream` means "do not copy the tree into a second full buffer", not "read files as they arrive".

The current CLI reads `KAGGLE_API_TOKEN` or `~/.kaggle/access_token`. It does not use the old `KAGGLE_KEY` and `kaggle.json`, so a token created in the Kaggle UI can stay Never used until `KAGGLE_API_TOKEN` is in `secrets.env` on the hub and the daemon is restarted.
