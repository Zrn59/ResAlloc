# Data: hub, direct, stream

**English** · [Русский](../data.md)

Code: `src/resalloc/data.py`, `src/resalloc/data_stores.py`, submit in `src/resalloc/client.py`.

The hub is the dispatcher and the checkpoint store. Its disk is shared with the operating system, so a dataset of hundreds of gigabytes should not be copied there. A Modal volume and a Kaggle dataset live at the provider.

| route | keep | What happens |
| --- | --- | --- |
| `hub` | `hub` | hash, cache on the hub, the provider receives a copy from the hub |
| `direct` | `modal` | the laptop uploads files into the `store` volume before the GPU starts |
| `direct` | `kaggle` | `kaggle datasets` reads the original folder; there is no second copy |
| `direct` | `runpod` / `vast` / `machine` | the host comes up, the script waits for `COMPLETE`, the laptop sends the bytes over SSH |
| `stream` | `modal` / `kaggle` | the same stores, in 8 MiB chunks, with a marker per file |
| `stream` | `runpod` | the pod is already training; `worker.iter_ready` sees files as they arrive |
| any | `laptop` | the bytes stay on the laptop; the request only records the path |

`store` is the Modal volume name or the Kaggle dataset slug. When it is empty, Modal uses `Modal.volume` or `resalloc-data`.

Stream markers: `name.done` sits next to each file, and `COMPLETE` is written at the end of the directory. Extra memory peaks at one chunk, not the whole dataset.

After a stop the hub pulls the checkpoint and the logs. Those are small files, not the dataset. On a move, the large dataset does not travel by itself: on Modal it stays on the volume, and for Runpod it has to be sent again from the laptop if the laptop is on.

A direct upload to Modal or Kaggle needs keys on the laptop. Keys on the hub are what the hub uses to start and stop machines.
