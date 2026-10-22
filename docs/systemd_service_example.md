# Optional systemd user service

This manual deployment example wraps a simtune command in a user service. It is
separate from the transient `--scope` that a tuning profile may request. simtune
does not install or enable this unit.

Prerequisites: an existing systemd user manager and a checkout installed into a
virtual environment. Adjust every path before use. This example assumes the
checkout is `~/simtune` and uses no root privileges.

Save as `~/.config/systemd/user/simtune-benchmark.service`:

```ini
[Unit]
Description=simtune example benchmark

[Service]
Type=oneshot
WorkingDirectory=%h/simtune
ExecStart=%h/simtune/.venv/bin/simtune profile --out %h/simtune/runs --repeat 3 -- %h/simtune/.venv/bin/python examples/cpu_bound.py
TimeoutStartSec=120
```

Start it explicitly and inspect the result:

```bash
systemctl --user daemon-reload
systemctl --user start simtune-benchmark.service
systemctl --user status simtune-benchmark.service
journalctl --user -u simtune-benchmark.service
```

This example has no install target and is not enabled at login. A workload
failure propagates through simtune to the service result. The run files contain
the captured workload output. Use the CLI's `--timeout` if each iteration also
needs its own limit; the unit timeout bounds the whole service.
