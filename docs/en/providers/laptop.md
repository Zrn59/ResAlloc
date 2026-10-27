# Laptop

**English** · [Русский](../../providers/laptop.md)

Code: `src/resalloc/providers/laptop.py`, `src/resalloc/laptop_agent.py`.

A free provider. The price in the policy is zero. The hub does not connect into the home network: `resalloc laptop agent` runs on the laptop and sends the heartbeat itself.

| Setting | Meaning |
| --- | --- |
| `cpus` | how many cores to claim, recorded on the job |
| `ram_gb` | how much memory to claim, recorded on the job |

If submit picks the laptop and there is no heartbeat, submit fails immediately.

`Dataset(..., keep="laptop")` does not copy the bytes. The script runs next to them.

Each `worker.save` under the SSH agent calls `resalloc laptop push`, so the checkpoint leaves for the hub at save time, not at the end of the process. A heartbeat missing longer than `laptop_heartbeat_timeout` is a break, the same as a vanished pod. `failover` continues from the last checkpoint that made it to the hub, when a next step is set.
