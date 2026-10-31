"""Command line. The Python API is the one training code should call."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

from resalloc.config import resolve
from resalloc.disk import DiskBudget, configured_reserve
from resalloc.hub import Hub, laptop_online
from resalloc.io import read_json
from resalloc.layout import Layout
from resalloc.providers.laptop import heartbeat_age_seconds, write_heartbeat
from resalloc.records import list_journals, protected_hashes, read_journal, write_command
from resalloc.setup import format_journal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="resalloc",
        epilog="Start here: resalloc help",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("help", help="short setup: this machine, or a laptop plus a hosting hub")
    config_cmd = sub.add_parser("config", help="show laptop and hub settings, never secret values")
    config_cmd.add_argument("--json", action="store_true")
    config_cmd.add_argument("--hub-only", action="store_true", help=argparse.SUPPRESS)
    up = sub.add_parser("up", help="start the hub here, or on --ssh-host")
    up.add_argument("--ssh-host", help="SSH Host from ~/.ssh/config. That machine becomes the hub.")
    up.add_argument("--hub-root", type=Path, help="hub directory. Default ~/resalloc, or ~/resalloc on the SSH host")
    up.add_argument("--extras", default="", help="modal, kaggle, or all, when --ssh-host installs the hub")
    up.add_argument("--force", action="store_true", help="point an existing config at --ssh-host")
    up.add_argument("--no-start", action="store_true", help="local only: prepare the directory without systemd")

    submit = sub.add_parser("submit", help="copy a job JSON into the hub inbox")
    submit.add_argument("job", type=Path)

    status = sub.add_parser("status", help="show one job or every job")
    status.add_argument("job_id", nargs="?")
    status.add_argument("--json", action="store_true")

    confirm = sub.add_parser("confirm", help="continue a parked job on the next provider")
    confirm.add_argument("job_id")

    edit = sub.add_parser("edit", help="change the next provider before confirm")
    edit.add_argument("job_id")
    edit.add_argument("--json", dest="payload")
    edit.add_argument("--gpu")
    edit.add_argument("--image")
    edit.add_argument("--accelerator")
    edit.add_argument("--spot", action="store_true")
    edit.add_argument("--no-spot", action="store_true")
    edit.add_argument("--disk-gb", type=int)
    edit.add_argument("--cpus", type=int)
    edit.add_argument("--ram-gb", type=float)
    edit.add_argument("--volume")
    edit.add_argument("--region")
    edit.add_argument("--cloud")
    edit.add_argument("--max-price", dest="max_price", type=float)
    edit.add_argument("--host")

    resume = sub.add_parser("resume", help="start a stopped job again from its checkpoint")
    resume.add_argument("job_id")

    attach = sub.add_parser("attach", help="follow a job and confirm the next provider from the terminal")
    attach.add_argument("job_id")
    attach.add_argument("--interval", type=float, default=5)

    init = sub.add_parser("init", help="write ~/.config/resalloc/config.toml")
    init.add_argument("--ssh-host", help="SSH Host that reaches the hub. Omit it to run the hub on this machine.")
    init.add_argument("--hub-root", type=Path, help="hub directory, default ~/resalloc")
    init.add_argument("--local", action="store_true", help="this machine is the hub")
    init.add_argument("--force", action="store_true", help="overwrite an existing config file")

    sub.add_parser("doctor", help="check this machine, or the hub behind ssh_host")
    test = sub.add_parser("test", help="run a one-minute GPU smoke test on selected providers")
    test.add_argument("providers", nargs="+", help="modal, kaggle, runpod, vast, laptop")
    test.add_argument("--timeout", type=float, default=900)

    hub = sub.add_parser("hub", help="run or install the hub daemon")
    hub_sub = hub.add_subparsers(dest="hub_command", required=True)
    hub_run = hub_sub.add_parser("run")
    hub_run.add_argument("--root", type=Path)
    hub_run.add_argument("--interval", type=float, default=60)
    hub_run.add_argument("--once", action="store_true")
    hub_install = hub_sub.add_parser("install", help="prepare the hub directory on this machine")
    hub_install.add_argument("--no-start", action="store_true", help="do not install or start a systemd unit")
    hub_boot = hub_sub.add_parser("bootstrap", help="install and start the hub on the SSH host")
    hub_boot.add_argument("--extras", default="", help="modal, kaggle, or all. Omitted by default so install stays fast")

    cache = sub.add_parser("cache")
    cache_sub = cache.add_subparsers(dest="cache_command", required=True)
    cache_has = cache_sub.add_parser("has")
    cache_has.add_argument("digest")
    cache_sub.add_parser("evict")
    cache_touch = cache_sub.add_parser("touch")
    cache_touch.add_argument("digest")
    cache_touch.add_argument("name")

    disk = sub.add_parser("disk")
    disk_sub = disk.add_subparsers(dest="disk_command", required=True)
    disk_sub.add_parser("info")

    jobs = sub.add_parser("jobs")
    jobs_sub = jobs.add_subparsers(dest="jobs_command", required=True)
    jobs_sub.add_parser("protected")
    jobs_ready = jobs_sub.add_parser("data-ready")
    jobs_ready.add_argument("job_id")
    jobs_ready.add_argument("--json", dest="payload", required=True)

    laptop = sub.add_parser("laptop")
    laptop_sub = laptop.add_subparsers(dest="laptop_command", required=True)
    laptop_sub.add_parser("heartbeat")
    laptop_sub.add_parser("poll")
    laptop_status = laptop_sub.add_parser("status")
    laptop_status.add_argument("--timeout", type=float, default=90)
    laptop_agent = laptop_sub.add_parser("agent")
    laptop_agent.add_argument("--interval", type=float, default=15)
    laptop_agent.add_argument("--once", action="store_true")
    laptop_push = laptop_sub.add_parser("push", help="send the latest checkpoint to the hub")
    laptop_push.add_argument("job_id")

    args = parser.parse_args(argv)
    if args.command is None:
        from resalloc.setup import quickstart

        print(quickstart())
        return 0
    try:
        return _dispatch(args)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "submit":
        from resalloc.client import ResAlloc
        from resalloc.models import Job, Migrate, limits_from_dict, target_from_dict

        raw = read_json(args.job)
        job = Job(
            script=str(raw["script"]),
            requirements=raw.get("requirements"),
            datasets=list(raw.get("datasets") or []),
            limits=limits_from_dict(raw.get("limits") or {}),
            start=target_from_dict(raw["start"]),
            failover=[
                Migrate(target_from_dict(step["target"]), confirm=bool(step.get("confirm", False)))
                for step in raw.get("failover") or []
            ],
            args=list(raw.get("args") or []),
            bundle_paths=[str(path) for path in raw.get("bundle_paths") or []],
            data_timeout_seconds=float(raw.get("data_timeout_seconds") or 3600),
            name=raw.get("name"),
        )
        print(ResAlloc.submit(job))
        return 0
    if args.command == "help":
        from resalloc.setup import quickstart

        print(quickstart())
        return 0
    if args.command == "config":
        return _show_config(args)
    if args.command == "up":
        return _up(args)
    if args.command == "init":
        return _init(args)
    if args.command == "doctor":
        return _doctor()
    if args.command == "test":
        return _test(args)
    if args.command == "attach":
        return _attach(args)
    if args.command == "hub":
        if args.hub_command == "bootstrap":
            return _bootstrap(args)
        return _hub(args)
    if args.command == "laptop" and args.laptop_command == "push":
        return _laptop_push(args.job_id)
    if args.command == "laptop" and args.laptop_command == "agent":
        return _agent(args)

    config = resolve()
    if not config.local and args.command in {"status", "confirm", "edit", "resume"}:
        return _remote(args)
    root = config.hub_root
    if args.command == "status":
        if args.job_id:
            payload = read_journal(root, args.job_id)
            if args.json:
                print(json.dumps(payload, indent=2))
            else:
                _print_journal(payload)
        else:
            rows = list_journals(root)
            if args.json:
                print(json.dumps(rows, indent=2))
            else:
                for row in rows:
                    _print_journal(row)
        return 0
    if args.command == "confirm":
        write_command(root, args.job_id, "confirm", {})
        return 0
    if args.command == "resume":
        write_command(root, args.job_id, "resume", {})
        return 0
    if args.command == "edit":
        write_command(root, args.job_id, "edit", edit_changes(args))
        return 0
    if args.command == "cache" and args.cache_command == "has":
        exists = (Layout(root).cache(args.digest) / "payload").exists()
        print("yes" if exists else "no")
        return 0
    if args.command == "cache" and args.cache_command == "evict":
        DiskBudget(root).evict(set(protected_hashes(root)))
        return 0
    if args.command == "cache" and args.cache_command == "touch":
        DiskBudget(root).touch(args.digest, args.name)
        return 0
    if args.command == "disk" and args.disk_command == "info":
        print(json.dumps(DiskBudget(root, configured_reserve(root=root)).info()))
        return 0
    if args.command == "jobs" and args.jobs_command == "protected":
        print(json.dumps(protected_hashes(root)))
        return 0
    if args.command == "jobs" and args.jobs_command == "data-ready":
        write_command(root, args.job_id, "data_ready", json.loads(args.payload))
        return 0
    if args.command == "laptop" and args.laptop_command == "heartbeat":
        write_heartbeat(root)
        return 0
    if args.command == "laptop" and args.laptop_command == "poll":
        rows = []
        for journal in list_journals(root):
            handle = journal.get("handle") or {}
            if journal.get("state") in {"running", "checkpointing"} and handle.get("provider") == "laptop":
                rows.append(journal)
        print(json.dumps(rows))
        return 0
    if args.command == "laptop" and args.laptop_command == "status":
        age = heartbeat_age_seconds(root)
        online = laptop_online(root, timedelta(seconds=args.timeout))
        print(json.dumps({"online": online, "age_seconds": age}))
        return 0
    raise SystemExit(f"unhandled command {args.command}")


def _remote(args: argparse.Namespace) -> int:
    from resalloc.client import ResAlloc

    client = ResAlloc()
    if args.command == "status":
        payload = client.status(args.job_id) if args.job_id else client.status()
        if args.json:
            print(json.dumps(payload, indent=2))
        elif isinstance(payload, list):
            for row in payload:
                _print_journal(row)
        else:
            _print_journal(payload)
        return 0
    if args.command == "confirm":
        client.confirm(args.job_id)
        return 0
    if args.command == "resume":
        client.resume(args.job_id)
        return 0
    client.edit(args.job_id, **edit_changes(args))
    return 0


def edit_changes(args: argparse.Namespace) -> dict:
    changes = json.loads(args.payload) if args.payload else {}
    if not isinstance(changes, dict):
        raise ValueError("--json must be an object")
    for key in ("gpu", "image", "accelerator", "volume", "region", "cloud", "host"):
        value = getattr(args, key)
        if value:
            changes[key] = value
    if args.spot:
        changes["spot"] = True
    if args.no_spot:
        changes["spot"] = False
    if args.disk_gb is not None:
        changes["disk_gb"] = args.disk_gb
    if args.cpus is not None:
        changes["cpus"] = args.cpus
    if args.ram_gb is not None:
        changes["ram_gb"] = args.ram_gb
    if args.max_price is not None:
        changes["max_price"] = args.max_price
    return changes


def _show_config(args: argparse.Namespace) -> int:
    from resalloc.config import config_path, load_file
    from resalloc.setup import describe_hub
    from resalloc.transport import SshTransport, make_transport

    config = resolve()
    if args.hub_only:
        print(json.dumps(describe_hub(config.hub_root)))
        return 0
    file_data = load_file()
    limits = file_data.get("limits") if isinstance(file_data.get("limits"), dict) else {}
    laptop = {
        "config": str(config_path()),
        "local": config.local,
        "ssh_host": config.ssh_host,
        "hub_root": str(config.hub_root),
        "limits": limits,
    }
    if config.local:
        hub = describe_hub(config.hub_root)
    else:
        transport = make_transport(config)
        if not isinstance(transport, SshTransport):
            raise RuntimeError("remote config needs an SSH host")
        hub = json.loads(transport._ssh(["resalloc", "config", "--hub-only"]))
    body = {"laptop": laptop, "hub": hub}
    if args.json:
        print(json.dumps(body, indent=2))
        return 0
    _print_config(body)
    return 0


def _print_config(body: dict) -> None:
    laptop = body["laptop"]
    hub = body["hub"]
    where = "this machine" if laptop.get("local") else f"ssh {laptop.get('ssh_host')}"
    print(f"config   {laptop.get('config')}")
    print(f"hub      {where}  {hub.get('hub_root') or laptop.get('hub_root')}")
    limits = laptop.get("limits") or {}
    if limits:
        print("limits   " + "  ".join(f"{key}={value}" for key, value in limits.items()))
    else:
        print("limits   defaults")
    caps = []
    for key in ("max_running", "tick_workers", "probe_failures"):
        value = hub.get(key)
        caps.append(f"{key}={'off' if value is None else value}")
    print("caps     " + "  ".join(caps))
    providers = hub.get("providers") or {}
    if providers:
        print("providers  " + "  ".join(f"{name}={cap}" for name, cap in providers.items()))
    secrets = hub.get("secrets") or []
    print("secrets  " + (", ".join(secrets) if secrets else "none"))
    reserve = hub.get("reserve_bytes")
    if reserve is not None:
        print(f"reserve  {reserve}")
    print("notify   " + ("on" if hub.get("notify") else "off"))


def _up(args: argparse.Namespace) -> int:
    from resalloc.config import config_path
    from resalloc.setup import NeedRemoteHome, SwitchHubError, bootstrap, init_config, install_local, remote_target
    from resalloc.setup import _remote_home

    path = config_path()
    if args.ssh_host or (path.exists() and resolve().ssh_host):
        return _up_remote(args, path, init_config, bootstrap, remote_target, _remote_home, SwitchHubError, NeedRemoteHome)
    if not path.exists():
        root = args.hub_root or (Path.home() / "resalloc")
        print(init_config(path, root, None))
    elif args.hub_root is not None:
        print(f"kept {path}")
    for note in install_local(resolve(), start=not args.no_start):
        if note:
            print(note)
    print("ready")
    return 0


def _up_remote(args, path, init_config, bootstrap, remote_target, remote_home, switch_error, need_home) -> int:
    if args.no_start:
        print("--no-start only applies to a hub on this machine.", file=sys.stderr)
        return 1
    existing_host = None
    existing_root = None
    if path.exists():
        current = resolve()
        existing_host = current.ssh_host
        existing_root = current.hub_root
    host = args.ssh_host or existing_host
    try:
        root, overwrite = remote_target(
            existing_host,
            existing_root,
            host,
            args.hub_root,
            force=args.force,
            remote_home=None,
        )
    except need_home:
        home = remote_home(
            resolve(hub_root=existing_root or Path.home() / "resalloc", ssh_host=host, prefer_local=False)
        )
        root, overwrite = remote_target(
            existing_host,
            existing_root,
            host,
            args.hub_root,
            force=args.force,
            remote_home=home,
        )
    except switch_error as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(init_config(path, root, host, force=overwrite))
    for note in bootstrap(resolve(), extras=args.extras):
        if note:
            print(note)
    print(f"hub {host}:{root}")
    print(f"keys: {root}/secrets.env on {host}")
    print("check: resalloc doctor")
    return 0


def _init(args: argparse.Namespace) -> int:
    from resalloc.config import config_path
    from resalloc.setup import init_config

    root = args.hub_root or (Path.home() / "resalloc")
    host = None if args.local or not args.ssh_host else args.ssh_host
    print(init_config(config_path(), root, host, force=args.force))
    return 0


def _hub(args: argparse.Namespace) -> int:
    if args.hub_command == "install":
        return _install_local(args)
    config = resolve()
    root = args.root or config.hub_root
    Hub(root).run(interval=args.interval, once=args.once)
    return 0


def _agent(args: argparse.Namespace) -> int:
    import time

    from resalloc.laptop_agent import LaptopAgent

    agent = LaptopAgent(resolve())
    while True:
        agent.once()
        if args.once:
            return 0
        time.sleep(args.interval)


def _print_journal(journal: dict) -> None:
    print(format_journal(journal))


def _install_local(args: argparse.Namespace) -> int:
    from resalloc.setup import install_local

    config = resolve()
    if not config.local:
        print(
            "this config points at an SSH hub. Use `resalloc hub bootstrap` for that machine, "
            "or `resalloc init --local --force` to make this machine the hub.",
            file=sys.stderr,
        )
        return 1
    for note in install_local(config, start=not args.no_start):
        if note:
            print(note)
    return 0


def _doctor() -> int:
    from resalloc.setup import checks_from_probe, local_checks, local_secret_report, probe_hub

    config = resolve()
    if config.local:
        rows = local_checks(config)
        failed = False
        for row in rows:
            mark = "ok" if row.ok else "!!"
            print(f"{mark}  {row.name}: {row.detail}")
            if not row.ok:
                failed = True
        return 1 if failed else 0
    probe, error = probe_hub(config)
    rows = checks_from_probe(config, probe, error)
    failed = False
    for row in rows:
        mark = "ok" if row.ok else "!!"
        print(f"{mark}  {row.name}: {row.detail}")
        if row.name != "secrets" and not row.ok:
            failed = True
    local = local_secret_report()
    present = [name for name, ok in local.items() if ok]
    if present:
        print("laptop has " + ", ".join(present) + "; the hub calls the APIs, so those keys belong in the hub secrets.env")
    return 1 if failed else 0


def _test(args: argparse.Namespace) -> int:
    from resalloc.client import ResAlloc
    from resalloc.smoke import parse_providers, run_smoke

    providers = parse_providers(args.providers)

    def on_line(text: str) -> None:
        print(text, flush=True)

    results = run_smoke(ResAlloc(), providers, timeout_seconds=args.timeout, on_line=on_line)
    return 1 if any(not result.get("ok") for result in results) else 0


def _bootstrap(args: argparse.Namespace) -> int:
    from resalloc.setup import bootstrap

    for note in bootstrap(resolve(), extras=args.extras):
        if note:
            print(note)
    return 0


def _attach(args: argparse.Namespace) -> int:
    import time

    from resalloc.client import ResAlloc
    from resalloc.setup import parse_edits

    client = ResAlloc()
    while True:
        journal = client.status(args.job_id)
        if not isinstance(journal, dict):
            raise RuntimeError("status did not return one job")
        print(format_journal(journal))
        state = journal.get("state")
        if state in {"stopped", "completed", "failed"}:
            return 0 if state != "failed" else 1
        if state == "awaiting_confirm":
            answer = input("confirm, edit key=value, or wait: ").strip()
            if answer == "confirm":
                client.confirm(args.job_id)
            elif answer.startswith("edit "):
                client.edit(args.job_id, **parse_edits(answer[5:]))
            elif answer in {"quit", "q"}:
                return 0
        time.sleep(args.interval)


def _laptop_push(job_id: str) -> int:
    from resalloc.setup import push_local_runtime

    push_local_runtime(job_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
