# Vast.ai

**English** · [Русский](../../providers/vast.md)

Code: `src/resalloc/providers/vast.py`.

REST `https://console.vast.ai/api/v0`. The key `VAST_API_KEY` lives on the hub. The SSH public key of that same account has to be registered on Vast before the first start: the API does not install a key into the instance. The hub private key is `VAST_SSH_IDENTITY` or `RESALLOC_SSH_IDENTITY`. A direct dataset upload needs that same key to open SSH from the laptop as well.

| Setting | Where it goes |
| --- | --- |
| `gpu` | `gpu_name`. `NVIDIA GeForce RTX 4090` becomes `RTX 4090` |
| `gpu_count` | `num_gpus` |
| `spot` | search `type=bid`; create sends `price` of the chosen offer |
| `image` | the image, otherwise `pytorch/pytorch:2.4.0-cuda12.4-cudnn9-runtime` |
| `disk_gb` | instance disk |
| `max_price` | hourly cap, otherwise `vast_max_hourly_usd` |
| `min_reliability` | `reliability` filter |
| `require_direct_ports` | at least one direct port, on by default |
| `env` | container environment |

Search is `POST /api/v0/bundles/`. Create is `PUT /api/v0/asks/{id}/` with `runtype=ssh` and `cancel_unavail`. `new_contract` in the response is the instance id. The hub waits until `actual_status` is `running` and `ssh_host` / `ssh_port` exist. `exited`, `unknown`, and `offline` never become `running` on their own: the instance is destroyed and the provider is skipped. Stop is `DELETE /api/v0/instances/{id}/`. A plain stop keeps billing for disk.

Money: `GET /api/v0/users/current` (`balance`) and `dph_total`. The session cap is `vast_max_spend_usd`, the floor is `vast_min_balance_usd`. Spend is the larger of the balance drop and `price × time`. The balance is cached for 30 seconds so a batch of jobs does not hit the rate limit. A `429` on reads and search is retried. Creating an instance is not retried: a retry could rent a second offer.

The script and the checkpoint travel over SSH the same way as on Runpod: directory `/resalloc`, the exit code is written to `finished.json`, and the instance is then destroyed.
