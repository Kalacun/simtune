"""Harmless real-process handshakes with bounded, unconditional test cleanup."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

WORKER = """import json,os,signal,subprocess,sys,time
from pathlib import Path
root=Path(sys.argv[1]); mode=sys.argv[2]
with (root/'launches').open('a') as stream: stream.write('launch\\n')
if mode == 'cooperative':
    def stop(signum, frame):
        (root/'forwarded').touch()
        print('cooperative-stop',flush=True)
        sys.exit(0)
    for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP): signal.signal(sig,stop)
else:
    def ignore(signum,frame):
        (root/'forwarded').touch()
    for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP): signal.signal(sig,ignore)
if mode in ('grandchild','escaped'):
    subprocess.Popen([sys.executable,__file__,str(root),'descendant'],
        start_new_session=mode=='escaped')
    while not (root/'descendant.json').exists(): time.sleep(.01)
print('partial-output',flush=True)
print('partial-error',file=sys.stderr,flush=True)
name='descendant.json' if mode=='descendant' else 'ready.json'
temporary=root/(name+'.tmp')
temporary.write_text(json.dumps({'pid':os.getpid(),'pgid':os.getpgrp()}))
temporary.replace(root/name)
deadline=time.monotonic()+30
while not (root/'release').exists() and time.monotonic()<deadline: time.sleep(.01)
(root/('descendant-completion' if mode=='descendant' else 'completion')).touch()
"""


def worker_argv(root, mode="cooperative"):
    script = root / "worker.py"
    script.write_text(WORKER)
    return [sys.executable, str(script), str(root), mode]


def wait_for(predicate, proc=None, seconds=10):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline or (proc is not None and proc.poll() is not None):
            raise AssertionError("process did not reach the expected readiness/liveness boundary")
        time.sleep(0.01)


def living(pid):
    # Reparented zombies cannot run or write a marker; init reaping differs by host.
    result = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=3
    )
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


def cleanup(proc, root):
    for name in ("ready.json", "descendant.json"):
        path = root / name
        if path.exists():
            data = json.loads(path.read_text())
            try:
                os.killpg(data["pgid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
            wait_for(lambda: not living(data["pid"]))
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=5)
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()


def signal_parent(root, driver, sig, *, repeated=False):
    """Signal only the simtune parent after its child explicitly announces readiness."""
    prefix = f"import sys; sys.path.insert(0, {str(ROOT / 'tests')!r})\n"
    proc = subprocess.Popen(
        [sys.executable, "-c", prefix + driver], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        try:
            wait_for(lambda: (root / "ready.json").exists(), proc)
        except AssertionError:
            if proc.poll() is not None:
                out, err = proc.communicate(timeout=3)
                raise AssertionError(f"parent exited before readiness: {out!r} {err!r}") from None
            raise
        ready = json.loads((root / "ready.json").read_text())
        proc.send_signal(sig)
        if repeated:
            wait_for(lambda: (root / "forwarded").exists())
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT if sig != signal.SIGINT else signal.SIGTERM)
        out, err = proc.communicate(timeout=8)
        # Assert before finally cleanup; cleanup must never hide an orphaning regression.
        wait_for(lambda: not living(ready["pid"]), seconds=3)
        descendant = root / "descendant.json"
        if descendant.exists():
            data = json.loads(descendant.read_text())
            if data["pgid"] == ready["pgid"]:
                wait_for(lambda: not living(data["pid"]), seconds=3)
            else:
                ready["escaped_alive_before_cleanup"] = living(data["pid"])
        return proc.returncode, out, err, ready
    finally:
        cleanup(proc, root)


def cancellation_workload(root, phase):
    """Pinned Python workload plus opt-in hooks/checks; host placement stays synthetic."""
    from dataclasses import replace

    from test_checks import command_check
    from test_search import make_workload

    from simtune.experiments import HookSpec, TrialExecutionOptions
    from simtune.workloads import load_workload, sha256_file

    loaded = make_workload(root / "manifest", checked=phase == "cleanup", heldout=False)
    workload_argv = (
        worker_argv(root, "ignoring")
        if phase == "workload"
        else [
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(root / 'workload-launch')!r}).touch(); import json; Path('result.json').write_text(json.dumps(dict(science=dict(value=6)))); print('workload-done')",
        ]
    )
    spec = replace(
        loaded.spec,
        argv_template=workload_argv,
        resolved_executable=sys.executable,
        executable_sha256=sha256_file(Path(sys.executable)),
        case_substitutions=[],
        cases=[replace(case, arguments={}) for case in loaded.spec.cases],
        artifacts=[],
        output_checks=[command_check(worker_argv(root, "ignoring"), timeout=20)]
        if phase == "validator"
        else loaded.spec.output_checks,
        limits=replace(loaded.spec.limits, trial_timeout_seconds=20),
    )
    loaded.manifest_path.write_text(spec.to_json())
    options = (
        TrialExecutionOptions(**{phase: HookSpec(tuple(worker_argv(root, "ignoring")), 20)})
        if phase in {"setup", "cleanup"}
        else TrialExecutionOptions()
    )
    return load_workload(loaded.manifest_path), options
