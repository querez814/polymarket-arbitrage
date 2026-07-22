# Systemd deployment

This definition runs exactly one Nightwatch process as an unprivileged user,
performs the non-network deployment preflight before startup, waits for process
liveness, restarts only on failure, and gives SIGTERM sixty seconds for the
runtime to halt and close its journals.

## Immutable release layout

- `/opt/nightwatch/releases/<git-sha>`: read-only source for one reviewed commit.
- `/opt/nightwatch/current`: symlink changed atomically during deployment.
- `/opt/nightwatch/releases/<git-sha>/.venv`: Python 3.12.13 environment synced
  from that release's hash-locked `requirements.lock` before it becomes current.
- `/etc/nightwatch/config.live.yaml`: untracked production configuration with
  absolute state/log paths and execution disabled until canary authorization;
  it is `root:nightwatch` mode `0640`, so preflight and runtime open the same
  service-readable but service-immutable path.
- `/etc/nightwatch/nightwatch.env`: `root:root` mode `0600` environment secrets.
- `/var/lib/nightwatch`: execution journal and operator state.
- `/var/log/nightwatch`: retained application logs.

The configuration should use absolute paths under `/var/lib/nightwatch` and
`/var/log/nightwatch`; otherwise systemd's read-only filesystem policy will
reject writes.

## Install

Create a dedicated `nightwatch` system account. The config, Kalshi key, state,
and log destinations must match the preflight contract. In particular:

```bash
chown root:root /etc/nightwatch/nightwatch.env
chmod 0600 /etc/nightwatch/nightwatch.env
chown root:nightwatch /etc/nightwatch/config.live.yaml
chmod 0640 /etc/nightwatch/config.live.yaml
chown nightwatch:nightwatch /etc/nightwatch/kalshi-private-key.pem
chmod 0600 /etc/nightwatch/kalshi-private-key.pem
chown nightwatch:nightwatch /var/lib/nightwatch /var/log/nightwatch
chmod 0700 /var/lib/nightwatch /var/log/nightwatch
```

Install CPython 3.12.13, then create the environment inside the immutable release
and enforce package hashes:

```bash
uv venv --python 3.12.13 /opt/nightwatch/releases/<git-sha>/.venv
UV_CACHE_DIR=/var/cache/nightwatch-uv uv pip sync \
  --require-hashes \
  --python /opt/nightwatch/releases/<git-sha>/.venv/bin/python requirements.lock
```

Copy `nightwatch.service` to `/etc/systemd/system/`, run `systemctl daemon-reload`,
and verify it before enabling:

```bash
systemd-analyze verify /etc/systemd/system/nightwatch.service
systemctl start nightwatch
systemctl status nightwatch
curl http://127.0.0.1:8888/health/live
curl http://127.0.0.1:8888/health/ready
```

Liveness proves only that the process is serving. Readiness requires the
configured matcher/scanner path to be initialized and alive. In execution-disabled
monitoring mode it does not require a production runtime; in canary mode it also
stays 503 while that runtime is operator-halted. A readiness failure is never a
supervisor restart condition. After recovery and alert delivery are verified, an
authenticated operator may arm the canary runtime and readiness should become 200.

For a separately approved canary, install `nightwatch-canary.conf.example` as a
reviewed systemd drop-in, replacing its two caps with the signed-off minimums.
The drop-in binds both preflight and `ExecStart` to the exact same canary config.
Record `systemctl cat nightwatch` and the canary config checksum before startup;
remove the drop-in and daemon-reload immediately after the final panic.

## Restart and rollback drill

With execution disabled, record the current journal/operator-state checksums,
send SIGTERM, verify a clean stop, start again, and confirm liveness returns.
The operator state must still be halted after restart. Roll back by atomically
repointing `/opt/nightwatch/current` to the prior reviewed release and restarting;
never roll back database files or run two versions against the same accounts.
