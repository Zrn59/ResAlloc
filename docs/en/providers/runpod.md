# Runpod

**English** · [Русский](../../providers/runpod.md)

Code: `src/resalloc/providers/runpod.py`.

REST v2, base `https://api.runpod.io`. The key `RUNPOD_API_KEY` lives on the hub. GraphQL is not used.

| Setting | Where it goes |
| --- | --- |
| `gpu`, `gpu_count` | `gpu.id`, `gpu.count` |
| `spot` | cloud `COMMUNITY`, unless `cloud` is set |
| `cloud` | `SECURE` or `COMMUNITY`, as given |
| `image` | the image, otherwise the official PyTorch image |
| `disk_gb` | container disk |
| `volume_gb` | persistent disk at `/data` |
| `network_volume_id` | network volume at `/data` |
| `data_centers` | `dataCenterIds` |
| `ports` | plus `22/tcp`, which SSH needs |
| `env` | container environment |

Create is `POST /v2/pods`. `entrypoint` and `cmd` are not sent: replacing them on `runpod/pytorch` drops `/start.sh`, and that script is what writes `PUBLIC_KEY` into `authorized_keys` and starts `sshd`. The script ends in `sleep infinity`, so the pod does not exit by itself. `env.PUBLIC_KEY` is the hub public key (`~/.ssh/id_ed25519.pub`), plus `startSsh`. After `RUNNING`, the adapter waits until direct SSH answers `echo ok` (the API publishes the port before sshd is listening) and only then copies files. The script runs in the background and writes `finished.json` when it exits. Otherwise the pod stays `RUNNING` and keeps billing. File copy uses `ssh.direct`: the proxy at `ssh.runpod.io` is a shell only and refuses rsync. When there is no direct address, both the upload and the checkpoint download are a tar stream through the shell. The stop signal uses the same path. After the checkpoint, stop is `DELETE`.

Money: `GET /v2/billing` and the price from `GET /v2/catalog/gpus`. The published v2 API has no live balance. If an account endpoint does return `balance`, `clientBalance`, or `creditBalance`, it is used together with `runpod_min_balance_usd`. Otherwise the cap is `runpod_max_spend_usd`. Spend is the larger of the billing delta and `price × time`.

A dataset with `keep="runpod"`: the laptop receives `data_endpoint.ssh` and uploads to the pod itself. `route="direct"` holds the script until `COMPLETE` appears. `route="stream"` starts the script immediately.
