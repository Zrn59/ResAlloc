# An existing SSH machine

**English** · [Русский](../../providers/machine.md)

Code: `src/resalloc/providers/machine.py`.

This is a host the hub can SSH to itself: a lab server, a VM you already rented, a second computer. The laptop is not this provider. The hub cannot reach a laptop through NAT, so the laptop dials out (`resalloc laptop agent`).

| Setting | Meaning |
| --- | --- |
| `host`, `user`, `port` | where to connect. The host name has no spaces and no shell characters |
| `identity` | private key on the hub. Without it, the default ssh key is used |
| `workdir` | directory on the machine, default `/var/tmp/resalloc/<job>` |
| `python` | interpreter, default `python3` |
| `gpu` | a label for the journal. The hub does not pick the GPU: it is already in the machine |

The price in the policy is zero. `stop` kills the process from `worker.pid` and does not power the machine off. Lost SSH is the same break as a vanished pod: the job moves to the next provider. A non-zero script exit is `worker_failed`, and the chain stops.

A direct dataset upload goes from the laptop to that same SSH host. The laptop key also has to be in `authorized_keys` when the bytes are not stored on the hub.
