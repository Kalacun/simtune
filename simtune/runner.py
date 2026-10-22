"""Command runner and action application."""

from __future__ import annotations

import os
import shlex
from collections.abc import Callable
from typing import Any, Optional

try:
    import resource
except ImportError:  # CLI help/doctor remain usable on unsupported hosts.
    resource = None

from . import capabilities as cap
from . import heuristics, metrics, profiler, profiles, storage, util


def _format_action(action: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in action.items()}


def _build_preexec(
    nice_level: Optional[int], rlimits: list[tuple[int, int, int]]
) -> Callable[[], None] | None:
    if nice_level is None and not rlimits:
        return None

    def _preexec() -> None:
        if nice_level is not None:
            os.setpriority(os.PRIO_PROCESS, 0, nice_level)
        for res, soft, hard in rlimits:
            resource.setrlimit(res, (soft, hard))

    return _preexec


def _ionice_args(action: dict[str, Any]) -> list[str]:
    cls = str(action.get("class", "best-effort")).lower()
    level = action.get("level")
    if cls == "idle":
        return ["-c", "3"]
    if cls in {"realtime", "real-time"}:
        args = ["-c", "1"]
        if level is not None:
            args += ["-n", str(level)]
        return args
    args = ["-c", "2"]
    if level is not None:
        args += ["-n", str(level)]
    return args


def _numactl_args(action: dict[str, Any]) -> list[str]:
    args = []
    if "interleave" in action:
        args.append(f"--interleave={action['interleave']}")
    if "membind" in action:
        args.append(f"--membind={action['membind']}")
    if "physcpubind" in action:
        args.append(f"--physcpubind={action['physcpubind']}")
    extra = action.get("args")
    if extra:
        args.extend(util.flatten_list(extra))
    return args


def _taskpolicy_args(action: dict[str, Any]) -> list[str]:
    args = []
    policy = action.get("policy")
    if policy:
        args.append("-b" if policy == "background" else str(policy))
    extra = action.get("args")
    if extra:
        args.extend(util.flatten_list(extra))
    return args


def _prepare_rlimits(
    action: dict[str, Any], applied: list, skipped: list
) -> list[tuple[int, int, int]]:
    limits = action.get("limits", {})
    if not isinstance(limits, dict):
        skipped.append({"action": _format_action(action), "reason": "limits must be a dict"})
        return []
    mapping = util.allowlist_resource_names()
    rlimits: list[tuple[int, int, int]] = []
    for name, value in limits.items():
        resource_id = mapping.get(name)
        if resource_id is None:
            skipped.append(
                {"action": _format_action(action), "reason": f"unsupported resource {name}"}
            )
            continue
        soft, hard = None, None
        if isinstance(value, (list, tuple)) and len(value) == 2:
            soft, hard = value
        else:
            soft = hard = value
        soft_i = util.parse_int(soft)
        hard_i = util.parse_int(hard)
        if soft_i is None or hard_i is None:
            skipped.append({"action": _format_action(action), "reason": f"invalid limit {name}"})
            continue
        # RLIM_INFINITY is -1 on Linux but a large positive sentinel on macOS.
        soft_i = resource.RLIM_INFINITY if soft_i == -1 else soft_i
        hard_i = resource.RLIM_INFINITY if hard_i == -1 else hard_i
        current_soft, current_hard = resource.getrlimit(resource_id)
        if (
            current_hard != resource.RLIM_INFINITY
            and (hard_i == resource.RLIM_INFINITY or hard_i > current_hard)
            and not util.is_root()
        ):
            skipped.append(
                {"action": _format_action(action), "reason": f"limit {name} exceeds hard cap"}
            )
            continue
        rlimits.append((resource_id, soft_i, hard_i))
    if rlimits:
        applied.append(
            {"action": _format_action(action), "detail": "rlimits scheduled via preexec"}
        )
    return rlimits


def build_action_plan(
    profile: dict[str, Any],
    capabilities: dict[str, Any],
    auto_mode: bool,
) -> dict[str, Any]:
    if not isinstance(profile, dict) or not isinstance(profile.get("actions"), list):
        raise ValueError("action plan requires a profile with an actions list")
    errors = [error for action in profile["actions"] for error in profiles.validate_action(action)]
    if errors:
        raise ValueError("Invalid action plan: " + "; ".join(errors))
    support_map = cap.supported_action_map(capabilities)
    tools = capabilities.get("tools", {})
    wsl = capabilities.get("is_wsl", False)

    requested = []
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    adjusted: list[dict[str, Any]] = []

    env_overrides: dict[str, str] = {}
    wrappers: list[list[str]] = []
    nice_level: Optional[int] = None
    rlimits: list[tuple[int, int, int]] = []

    systemd_props: dict[str, str] = {}
    use_systemd = any(
        action.get("type") == "cgroup_systemd"
        and support_map.get("cgroup_systemd", False)
        and tools.get("systemd-run", {}).get("path")
        for action in profile.get("actions", [])
    )

    for action in profile.get("actions", []):
        requested.append(_format_action(action))
        action_type = action.get("type")
        supported = support_map.get(action_type, False)

        if action_type == "numactl" and auto_mode and wsl:
            skipped.append(
                {"action": _format_action(action), "reason": "WSL2 auto mode avoids NUMA binding"}
            )
            continue

        if not supported:
            skipped.append(
                {
                    "action": _format_action(action),
                    "reason": f"{action_type} unavailable on this host",
                }
            )
            continue

        if action_type == "env":
            env_vars = action.get("vars", {})
            if not isinstance(env_vars, dict):
                skipped.append({"action": _format_action(action), "reason": "vars must be a dict"})
                continue
            env_overrides.update({str(k): str(v) for k, v in env_vars.items()})
            applied.append({"action": _format_action(action), "detail": "environment overrides"})
            continue

        if action_type == "nice":
            level = util.parse_int(action.get("level"))
            if level is None:
                skipped.append({"action": _format_action(action), "reason": "invalid nice level"})
                continue
            current_nice = os.getpriority(os.PRIO_PROCESS, 0)
            if level < current_nice and not util.is_root():
                adjusted.append(
                    {
                        "action": _format_action(action),
                        "reason": "priority increase may require privileges; preserving inherited nice level",
                        "applied_as": {"type": "nice", "level": current_nice},
                    }
                )
                level = current_nice
            # A scope inherits the caller's process settings, unlike a service.
            nice_level = level
            applied.append(
                {"action": {**_format_action(action), "level": level}, "detail": "preexec priority"}
            )
            continue

        if action_type == "ulimit":
            rlimits.extend(_prepare_rlimits(action, applied, skipped))
            continue

        if action_type == "ionice":
            ionice_path = tools.get("ionice", {}).get("path")
            if not ionice_path:
                skipped.append({"action": _format_action(action), "reason": "ionice not found"})
                continue
            args = _ionice_args(action)
            wrappers.append([ionice_path] + args + ["--"])
            applied.append({"action": _format_action(action), "detail": "ionice wrapper"})
            continue

        if action_type == "taskset":
            taskset_path = tools.get("taskset", {}).get("path")
            cpu_info = capabilities.get("cpu", {})
            allowed = cpu_info.get("affinity")
            if allowed is None and cpu_info.get("logical_count"):
                allowed = list(range(cpu_info["logical_count"]))
            requested_cpus = action["cpus"]
            if requested_cpus == "available":
                chosen = set(allowed or [])
            else:
                chosen = profiles.parse_cpu_list(requested_cpus)
                if allowed is not None:
                    chosen &= set(allowed)
            if not taskset_path or not chosen:
                skipped.append(
                    {
                        "action": _format_action(action),
                        "reason": "no requested CPUs are known to be available",
                    }
                )
                continue
            cpus = ",".join(str(cpu) for cpu in sorted(chosen))
            if requested_cpus == "available" or (
                allowed is not None and chosen != profiles.parse_cpu_list(requested_cpus)
            ):
                adjusted.append(
                    {
                        "action": _format_action(action),
                        "reason": "resolved against available CPU affinity",
                        "applied_as": {"type": "taskset", "cpus": cpus},
                    }
                )
            # taskset stops option parsing at the CPU list: `--` must precede it.
            wrappers.append([taskset_path, "-c", "--", cpus])
            applied.append(
                {"action": {**_format_action(action), "cpus": cpus}, "detail": "taskset wrapper"}
            )
            continue

        if action_type == "numactl":
            numactl_path = tools.get("numactl", {}).get("path")
            if not numactl_path:
                skipped.append({"action": _format_action(action), "reason": "numactl not found"})
                continue
            args = _numactl_args(action)
            wrappers.append([numactl_path] + args + ["--"])
            applied.append({"action": _format_action(action), "detail": "numactl wrapper"})
            continue

        if action_type == "cgroup_systemd":
            systemd_run = tools.get("systemd-run", {}).get("path")
            if not systemd_run:
                skipped.append(
                    {"action": _format_action(action), "reason": "systemd-run not found"}
                )
                continue
            use_systemd = True
            props = action.get("properties", {})
            if isinstance(props, dict):
                systemd_props.update({str(k): str(v) for k, v in props.items()})
            applied.append({"action": _format_action(action), "detail": "systemd-run wrapper"})
            continue

        if action_type == "mac_taskpolicy":
            taskpolicy_path = tools.get("taskpolicy", {}).get("path")
            if not taskpolicy_path:
                skipped.append({"action": _format_action(action), "reason": "taskpolicy not found"})
                continue
            args = _taskpolicy_args(action)
            wrappers.append([taskpolicy_path] + args + ["--"])
            applied.append({"action": _format_action(action), "detail": "taskpolicy wrapper"})
            continue

        skipped.append({"action": _format_action(action), "reason": "unknown action type"})

    if use_systemd:
        systemd_run = tools.get("systemd-run", {}).get("path")
        if systemd_run:
            props = []
            for key, value in systemd_props.items():
                props.append(f"--property={key}={value}")
            wrappers.insert(
                0,
                [systemd_run, "--user", "--scope", "--quiet", "--expand-environment=no"]
                + props
                + ["--"],
            )

    preexec = _build_preexec(nice_level, rlimits)

    return {
        "requested": requested,
        "applied": applied,
        "skipped": skipped,
        "adjusted": adjusted,
        "wrappers": wrappers,
        "env_overrides": env_overrides,
        "preexec": preexec,
    }


def _combine_command(wrappers: list[list[str]], cmd: list[str]) -> list[str]:
    combined: list[str] = []
    for wrapper in wrappers:
        combined.extend(wrapper)
    combined.extend(cmd)
    return combined


def _check_required_actions(profile: dict[str, Any], support_map: dict[str, bool]) -> list[str]:
    missing: list[str] = []
    for action in profile.get("actions", []):
        if action.get("optional"):
            continue
        action_type = action.get("type")
        if not support_map.get(action_type, False):
            missing.append(action_type)
    return missing


def select_profile(
    profiles_data: dict[str, Any],
    mode: str,
    capabilities: dict[str, Any],
) -> tuple[str, dict[str, Any], list[str]]:
    support_map = cap.supported_action_map(capabilities)
    reasons: list[str] = []
    visited = set()

    current = mode
    while True:
        if current in visited:
            break
        visited.add(current)
        profile = profiles.get_profile(profiles_data, current)
        missing = _check_required_actions(profile, support_map)
        if not missing:
            return current, profile, reasons
        fallback = profile.get("fallback_mode")
        reasons.append(f"Missing capabilities {missing}; fallback to {fallback}")
        if not fallback or fallback not in profiles_data:
            return current, profile, reasons
        current = fallback

    reasons.append(f"Fallback cycle detected; using supported subset of {mode}")
    return mode, profiles.get_profile(profiles_data, mode), reasons


@util.cancellable
def run_command(
    cmd: list[str],
    out_dir: str,
    mode: str,
    profiles_data: dict[str, Any],
    capabilities: dict[str, Any],
    timeout: Optional[float],
    repeat: int,
    dry_run: bool,
    auto_mode: bool,
    latency_important: bool,
    respect_desktop: bool,
    baseline_metrics: Optional[dict[str, Any]] = None,
    notify: bool = False,
    baseline_run_dir: Optional[str] = None,
) -> Optional[str]:
    cmd = util.sanitize_cmd(cmd)
    util.validate_run_options(repeat, timeout)
    if capabilities.get("platform") not in {"linux", "macos"}:
        raise ValueError("workload execution requires Linux (including WSL) or macOS")
    selected_mode = mode
    rationale: dict[str, Any] = {}

    if auto_mode:
        if baseline_metrics and (
            baseline_metrics.get("timed_out") or baseline_metrics.get("return_code", 0) != 0
        ):
            raise ValueError("auto mode requires a successful baseline")
        if not baseline_metrics and not dry_run:
            raise ValueError("auto mode requires baseline metrics")
        decision = heuristics.select_mode(
            baseline_metrics or {},
            capabilities,
            latency_important,
            respect_desktop,
        )
        selected_mode = decision["mode"]
        rationale = decision
        rationale["baseline_metrics"] = baseline_metrics
        rationale["baseline_run_dir"] = baseline_run_dir
        if not baseline_metrics:
            rationale["provisional"] = True

    selected_mode, profile, fallback_reasons = select_profile(
        profiles_data, selected_mode, capabilities
    )
    if fallback_reasons:
        rationale.setdefault("fallbacks", []).extend(fallback_reasons)

    rationale["selected_mode"] = selected_mode
    action_plan = build_action_plan(profile, capabilities, auto_mode=auto_mode)
    final_cmd = _combine_command(action_plan["wrappers"], cmd)

    if dry_run:
        if auto_mode and not baseline_metrics:
            print("Provisional auto plan: no baseline was executed.")
        print("Action plan:")
        print(util.format_kv({"mode": selected_mode, "profile": selected_mode}))
        print(
            util.render_table(action_plan["applied"], ["action", "detail"])
            if action_plan["applied"]
            else "No actions applied"
        )
        if action_plan["skipped"]:
            print(util.render_table(action_plan["skipped"], ["action", "reason"]))
        if action_plan["adjusted"]:
            print(util.render_table(action_plan["adjusted"], ["action", "reason", "applied_as"]))
        for reason in fallback_reasons:
            print(reason)
        print("Final command:")
        print(shlex.join(final_cmd))
        return None

    for reason in fallback_reasons:
        print(f"Profile resolution: {reason}")
    for kind in ("skipped", "adjusted"):
        for item in action_plan[kind]:
            print(f"Action {kind}: {item['action']['type']}: {item['reason']}")
    run_dir = storage.create_run_dir(out_dir, selected_mode)

    env = util.apply_env_overrides(util.default_env(), action_plan["env_overrides"])

    iteration_metrics: list[dict[str, Any]] = []
    raw_outputs: list[str] = []

    for idx in range(repeat):
        run_metrics, raw_output = profiler.profile_command(
            final_cmd,
            env,
            timeout,
            capabilities,
            preexec_fn=action_plan["preexec"],
        )
        run_metrics["iteration"] = idx + 1
        iteration_metrics.append(metrics.normalize_metrics(run_metrics))
        raw_outputs.append(raw_output)
        if (run_metrics.get("cancellation") or {}).get("parent_signal") is not None:
            break

    aggregated = metrics.aggregate_metrics(iteration_metrics)

    notification_result = {"enabled": notify, "applied": False, "reason": ""}
    if notify:
        summary = f"simtune run ({selected_mode})"
        wall_mean = aggregated.get("wall_time_sec_mean")
        body = f"wall_time_sec_mean={wall_mean}" if wall_mean is not None else "run complete"
        applied, reason = util.send_notification(summary, body)
        notification_result.update({"applied": applied, "reason": reason})

    applied_actions = {
        "requested": action_plan["requested"],
        "applied": action_plan["applied"],
        "skipped": action_plan["skipped"],
        "adjusted": action_plan["adjusted"],
        "final_command": final_cmd,
        "notifications": notification_result,
        "execution_status": "unknown"
        if any(
            not (item.get("cancellation") or {}).get("termination_known", True)
            for item in iteration_metrics
        )
        else "interrupted"
        if any((item.get("cancellation") or {}).get("parent_signal") for item in iteration_metrics)
        else "succeeded"
        if aggregated["failed_count"] == 0
        else "failed",
        "note": "applied records planned settings; wrapper effects are not independently verified",
    }

    meta = {
        "requested_mode": mode,
        "mode": selected_mode,
        "cmd": cmd,
        "timestamp": util.iso_timestamp(),
        "repeat": repeat,
        "timeout": timeout,
        "auto_mode": auto_mode,
        "latency_important": latency_important,
        "respect_desktop": respect_desktop,
        "platform": capabilities.get("platform"),
        "is_wsl": capabilities.get("is_wsl"),
    }

    storage.write_run(
        run_dir,
        meta=meta,
        capabilities=capabilities,
        applied_actions=applied_actions,
        metrics={"iterations": iteration_metrics, "aggregate": aggregated},
        raw_profiler="\n\n---\n\n".join(raw_outputs),
        cmd=cmd,
        rationale=rationale,
    )

    return str(run_dir)
