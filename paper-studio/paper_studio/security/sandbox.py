"""Private Linux build namespaces adapted from prior project-scoped workers.

Maintainer provenance records identify the original worker revisions.
The public implementation uses registered package workers and generic paths.
Only fixed package workers run here; there is deliberately no host fallback.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid

PROGRAMS = {"latex": "latex/worker.py", "figure": "figures/worker.py", "pdf": "pdf/worker.py"}
DEFAULT_LIMITS = {"timeout_seconds": 180, "max_output_bytes": 67108864, "max_files": 2048,
                  "max_memory_bytes": 4 * 1024**3, "max_processes": 128, "cpu_quota_percent": 100}


class SandboxUserBusError(RuntimeError):
    """The required current-user systemd bus cannot be resolved safely."""


def user_bus_environment() -> dict[str, str]:
    """Recover missing launcher variables without mutating the host environment.

    Tunnel-launched Gateways may lack the CLI's session variables. Only the
    existing current-UID runtime directory and owned Unix socket are eligible;
    this does not start services, use a system bus, or enable host execution.
    """
    inherited = {key: os.environ[key] for key in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
                 if os.environ.get(key)}
    if len(inherited) == 2:
        return inherited
    uid = os.getuid()
    runtime = Path("/run/user") / str(uid)
    bus = runtime / "bus"
    try:
        directory = runtime.lstat()
        socket = bus.lstat()
        valid = (runtime.resolve() == runtime and bus.resolve() == bus
                 and stat.S_ISDIR(directory.st_mode) and directory.st_uid == uid
                 and not directory.st_mode & 0o022
                 and stat.S_ISSOCK(socket.st_mode) and socket.st_uid == uid)
    except OSError:
        valid = False
    if not valid:
        raise SandboxUserBusError("A verified current-user session bus is required for isolated execution; host fallback remains disabled")
    return {"XDG_RUNTIME_DIR": str(runtime), "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + str(bus), **inherited}


def relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("Expected a nonempty project-relative POSIX path")
    path = Path(value)
    if path.is_absolute() or any(p in {".", ".."} for p in value.split("/")) or value.startswith("-"):
        raise ValueError("Unsafe project-relative path")
    return path.as_posix()


def tree_usage(root: Path, limits: dict) -> tuple[int, int]:
    count = total = 0
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            item = Path(parent) / name
            try:
                info = item.lstat()
            except FileNotFoundError:
                # Engines atomically replace their own auxiliary files while
                # the parent monitors quotas; the final scan runs after exit.
                continue
            count += 1
            if count > limits["max_files"]:
                raise RuntimeError("Filesystem entry quota exceeded")
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise ValueError("Symlinks and nonregular files are forbidden")
            if stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    relative = item.relative_to(root).as_posix()
                    raise ValueError(f"Hardlinks are forbidden (path={relative!r}, nlink={info.st_nlink})")
                total += info.st_size
            if total > limits["max_output_bytes"]:
                raise RuntimeError("Output byte quota exceeded")
    return count, total


def _absolute(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("Expected an absolute path without aliases or symlinks")
    return path


def _cgroup_limits(unit: str, limits: dict, environment: dict | None = None) -> dict | None:
    """Read actual kernel settings while the private service still exists."""
    probe = subprocess.run([shutil.which("systemctl"), "--user", "show", "--property=ControlGroup", "--value", unit],
                           capture_output=True, text=True, timeout=3, env=environment)
    group = probe.stdout.strip()
    if probe.returncode or not group:
        return None
    path = Path("/sys/fs/cgroup") / group.lstrip("/")
    try:
        values = {key: (path / key).read_text().strip()
                  for key in ("memory.max", "memory.swap.max", "pids.max", "cpu.max")}
    except FileNotFoundError:
        return None
    if values["memory.max"] != str(int(limits["max_memory_bytes"])) or values["pids.max"] != str(int(limits["max_processes"])) or values["memory.swap.max"] != "0":
        raise RuntimeError("Kernel cgroup process-tree limits do not match the requested budget")
    quota, period = values["cpu.max"].split()
    if quota == "max" or int(quota) / int(period) > limits["cpu_quota_percent"] / 100 + 0.001:
        raise RuntimeError("Kernel cgroup CPU quota is not enforced")
    return {"path": group, "kernel_settings": values}


def run_sandbox(snapshot: Path, output: Path, program: str, request: dict,
                config: dict, *, tex: bool = False) -> dict:
    """Run a registered worker with immutable /input and writable /work.

    Config is trusted provider configuration, never copied from client requests.
    Output must be fresh or empty. Even failure retains stdout and execution.json.
    """
    if sys.platform != 'linux':
        raise RuntimeError('Isolated paper builds require Linux or WSL 2; configure a Linux build host')
    if program not in PROGRAMS:
        raise ValueError("Unregistered build worker")
    snapshot, output = _absolute(snapshot), _absolute(output)
    if not snapshot.is_dir() or snapshot == output or snapshot in output.parents or output in snapshot.parents:
        raise ValueError("Input and output must be disjoint directories")
    limits = {**DEFAULT_LIMITS, **config.get("limits", {})}
    for key in DEFAULT_LIMITS:
        value = limits[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError("Limits must be finite positive numbers")
        if key not in {"timeout_seconds", "cpu_quota_percent"} and not isinstance(value, int):
            raise ValueError("Byte and task limits must be positive integers")
    limits["max_files"] = int(limits["max_files"])
    limits["max_output_bytes"] = int(limits["max_output_bytes"])
    tree_usage(snapshot, limits)
    toolchain = config.get("toolchain", {})
    prefix = Path(toolchain.get("python_prefix", sys.prefix)).resolve()
    if prefix != Path(sys.prefix).resolve() or not (prefix / "bin/python").is_file():
        raise RuntimeError("Worker must run in the configured managed Python prefix")
    if not Path("/usr/bin/bwrap").is_file():
        raise RuntimeError("bubblewrap is required; host execution fallback is disabled")
    if not shutil.which("systemd-run") or not shutil.which("systemctl"):
        raise RuntimeError("systemd user cgroups are required for process-tree limits; fallback is disabled")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise FileExistsError("Output must be a fresh empty private job directory")
    output.mkdir(parents=True, exist_ok=True)
    source_inventory = {p.relative_to(snapshot).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in sorted(snapshot.rglob("*")) if p.is_file()}
    (output / "request.json").write_text(json.dumps(request, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    (output / "source_manifest.json").write_text(json.dumps(source_inventory, indent=2), encoding="utf-8")
    (output / "stdout.log").touch(exist_ok=False)
    command = ["/usr/bin/bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL", "--clearenv"]
    for path in ("/usr", "/lib", "/lib64"):
        if Path(path).exists():
            command += ["--ro-bind", path, path]
    command += ["--symlink", "usr/bin", "/bin", "--ro-bind", str(prefix), "/opt/python",
                "--ro-bind", str(Path(__file__).resolve().parents[1]), "/code",
                "--ro-bind", str(snapshot), "/input", "--bind", str(output), "/work",
                "--proc", "/proc", "--dev", "/dev", "--size", str(256 * 1024**2),
                "--tmpfs", "/tmp", "--chdir", "/input"]
    # Receipts and stdout are host-owned: user scripts cannot rewrite evidence.
    for protected in ("request.json", "source_manifest.json", "stdout.log"):
        command += ["--ro-bind", str(output / protected), "/work/" + protected]
    addons = toolchain.get("python_addons")
    if addons:
        addons = _absolute(Path(addons))
        if not addons.is_dir():
            raise RuntimeError("Configured isolated Python addons directory is missing")
        command += ["--ro-bind", str(addons), "/opt/addons"]
    path_env = "/opt/python/bin:/usr/bin:/bin"
    if tex:
        root = toolchain.get("texlive_root")
        if not root or not Path(root).is_dir():
            raise RuntimeError("Explicit installed texlive_root required")
        command += ["--ro-bind", str(Path(root).resolve()), "/texlive"]
        path_env = "/texlive/bin/x86_64-linux:" + path_env
    fontroot = toolchain.get("windows_fonts_root")
    if fontroot:
        if not Path(fontroot).is_dir():
            raise RuntimeError("Configured fonts directory is missing")
        command += ["--ro-bind", str(Path(fontroot).resolve()), "/fonts/windows"]
    (output / "fontconfig.xml").write_text(
        '<fontconfig><dir>/usr/share/fonts</dir><dir>/fonts/windows</dir>'
        '<dir>/texlive/texmf-dist/fonts/opentype</dir>'
        '<cachedir>/work/.fontcache</cachedir></fontconfig>', encoding="utf-8")
    environment = {"PATH": path_env, "HOME": "/work/home", "TMPDIR": "/tmp", "LANG": "C.UTF-8",
                   "LC_ALL": "C.UTF-8", "MPLCONFIGDIR": "/work/.matplotlib", "MPLBACKEND": "Agg",
                   "FONTCONFIG_FILE": "/work/fontconfig.xml", "XDG_CACHE_HOME": "/work/.cache",
                   "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
                   "PYTHONDONTWRITEBYTECODE": "1", "PAPER_OUTPUT_DIR": "/work",
                   "PAPER_PARAMETERS": json.dumps(request.get("parameters", {}), allow_nan=False)}
    for key, value in environment.items():
        command += ["--setenv", key, value]
    command += ["--remount-ro", "/", "/opt/python/bin/python", "-I", "-B", "/code/" + PROGRAMS[program], "/work/request.json"]

    unit = "paper-studio-build-" + uuid.uuid4().hex + ".service"
    # systemd applies these before exec; every descendant inherits the same
    # cgroup. RLIMIT_AS/CPU below are explicitly per-process, supplementary
    # constraints, never advertised as aggregate tree limits.
    seconds = max(1, math.ceil(limits["timeout_seconds"]))
    service_properties = {"MemoryMax": str(int(limits["max_memory_bytes"])), "MemorySwapMax": "0",
                          "TasksMax": str(int(limits["max_processes"])), "CPUQuota": str(limits["cpu_quota_percent"]) + "%",
                          "KillMode": "control-group", "OOMPolicy": "kill", "TimeoutStopSec": "5s",
                          "RuntimeMaxSec": str(max(10, seconds + 5)), "LimitCORE": "0", "LimitAS": str(4 * 1024**3),
                          "LimitFSIZE": str(limits["max_output_bytes"]), "LimitNOFILE": "256", "LimitCPU": str(seconds),
                          "UMask": "0077"}
    launcher = [shutil.which("systemd-run"), "--user", "--quiet", "--wait", "--pipe", "--collect",
                "--expand-environment=no", "--unit=" + unit]
    launcher += ["--property=" + key + "=" + value for key, value in service_properties.items()]
    command = launcher + command

    started = time.monotonic()
    record = {"program": program, "limits": limits, "isolation": {"network": "unshared",
              "input": "read-only", "host_home": "absent", "host_fallback": False,
              "private_temporary_bytes": 256 * 1024**2, "stdout": "parent-owned pipe capture",
              "process_tree": {"backend": "systemd user cgroup v2", "unit": unit, "properties": service_properties},
              "per_process_rlimits": ["LimitAS", "LimitCPU", "LimitFSIZE", "LimitNOFILE"]}, "status": "running"}
    process = None
    selector = selectors.DefaultSelector()
    log_bytes = 0
    try:
        with (output / "stdout.log").open("wb", buffering=0) as log:
            # The child receives a pipe, never a writable log-file descriptor.
            # Preserve only the variables needed to contact the local user bus;
            # bwrap independently clears its actual worker environment.
            launch_env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
            launch_env.update(user_bus_environment())
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       start_new_session=True, close_fds=True, env=launch_env)
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            while process.poll() is None or selector.get_map():
                tree_usage(output, limits)
                if "verified" not in record["isolation"]["process_tree"]:
                    observed = _cgroup_limits(unit, limits, launch_env)
                    if observed:
                        record["isolation"]["process_tree"]["verified"] = observed
                if time.monotonic() - started > limits["timeout_seconds"]:
                    raise TimeoutError("Sandbox wall time exceeded")
                for key, _ in selector.select(timeout=0.1):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    available = max(0, limits["max_output_bytes"] - tree_usage(output, limits)[1])
                    kept = chunk[:available]
                    log.write(kept)
                    log_bytes += len(kept)
                    if len(kept) < len(chunk):
                        raise RuntimeError("Output byte quota exceeded by stdout/stderr")
            record["returncode"] = process.returncode
            tree_usage(output, limits)
            if process.returncode:
                raise RuntimeError("Isolated worker failed; inspect stdout.log")
        after = {p.relative_to(snapshot).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(snapshot.rglob("*")) if p.is_file()}
        if source_inventory != after:
            raise RuntimeError("Source snapshot changed during build")
        record["status"] = "completed"
        return record
    except BaseException as exc:
        record.update(status="failed", error=str(exc))
        raise
    finally:
        # Kill the whole named cgroup, including reparented/setsid descendants.
        # A completed --collect service may already have been unloaded.
        if process is not None and process.poll() is None:
            try:
                cleanup = subprocess.run([shutil.which("systemctl"), "--user", "kill", "--kill-whom=all", "--signal=KILL", unit],
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, env=launch_env)
                record["cgroup_cleanup_returncode"] = cleanup.returncode
                if cleanup.returncode:
                    record["recovery_required"] = True
            except (OSError, subprocess.TimeoutExpired) as cleanup_error:
                record["recovery_required"] = True
                record["cgroup_cleanup_error"] = str(cleanup_error)
        if process is not None and process.poll() is None:
            try:
                # --wait exits after the service stops, confirming tree cleanup.
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                record["recovery_required"] = True
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=20)
        selector.close()
        if process is not None and process.stdout is not None:
            process.stdout.close()
        record["stdout_bytes"] = log_bytes
        record["elapsed_seconds"] = time.monotonic() - started
        # Replace instead of following a hostile output symlink after failure.
        with tempfile.NamedTemporaryFile(mode="w", dir=output, prefix=".receipt-", delete=False) as receipt:
            json.dump(record, receipt, indent=2)
            receipt_path = receipt.name
        os.replace(receipt_path, output / "execution.json")
