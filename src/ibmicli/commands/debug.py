"""Offline launch configuration for the official IBM i Debug extension.

Resolve the source's app, environment and object library. The extension
passes the JSON configuration directly to startDebugging; passwords remain
in memory and never appear in normal output or launch.json.

Use the TLS hostname in debug_host. The IBM extension verifies certificates
on the new connection opened when a service entry point is hit; an IP fails.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import ssl
from pathlib import Path

from .. import output, project as prj
from ..config import Config, ConfigError, Env
from ..naming import split_file
from ..tasks import CL, Task, kind_of, problems
from . import run as run_cmd

# Default Debug Service ports: TLS on 8005,
# service-entry-point daemon on 8008.
# Keep these constants until configurable ports are needed.
DEBUG_PORT = 8005
SEP_DAEMON_PORT = 8008

# Debug assumes a *PGM and a single same-named module,
# matching single-module compilation tasks.
PROGRAM_TYPE = "*PGM"

# Example proposed when debug_batch is missing.
# It becomes configuration only when explicitly accepted
# in set-conn; it is not a runtime default.
BATCH_EXAMPLE = (
    "SBMJOB CMD(CALL PGM({objlib}/{member})) JOBQ(QSYS/QSYSNOMAX) "
    "MSGQ(*USRPRF) CPYENVVAR(*YES)"
)


def run(
    cfg: Config,
    source: Path,
    sep: bool = False,
    as_json: bool = False,
    project: prj.Project | None = None,
) -> int:
    if not source.is_file():
        output.error(f"{source}: file not found")
        return 2

    host = _host(cfg)
    found = prj.resolve(cfg, project, source)
    # Only the project supplies library and environment; no path guessing.
    if found is None:
        if project is None:
            raise prj.no_project()
        raise ConfigError(
            f"{source} is outside APP/LIBRARY/SOURCEFILE in {project.path}\n"
            "       debug requires a source in a declared project app"
        )
    env = found.env
    member, ext = split_file(source)
    library = prj.object_library(found.app, env)
    if not library:
        raise prj.missing_library(found.app, env)
    # A SEP submits no job, so a batch command is not required.
    batch = None if sep else _batch(cfg, env, source, member, ext, found)

    data = {
        "file": str(source.resolve()),
        "env": env.name if env is not None else "",
        "host": host,
        "port": DEBUG_PORT,
        "sepDaemonPort": SEP_DAEMON_PORT,
        "user": cfg.user.upper(),
        "password": cfg.password,
        "library": library,
        "program": member,
        "module": member,
        "programType": PROGRAM_TYPE,
    }
    if batch is not None:
        data["startBatchJobCommand"] = batch

    if as_json:
        print(json.dumps(data, ensure_ascii=False))
        return 0

    output.info(f"debug  env {data['env'] or '-'}  ->  {library}/{member}  "
                f"({PROGRAM_TYPE}, module {member})")
    output.info(f"Debug Service:  {host}:{DEBUG_PORT}  (TLS, certificate verified)  "
                f"user {data['user']}")
    if batch is not None:
        output.info(f"batch:          {batch}")
    else:
        output.info(f"SEP:            daemon on port {SEP_DAEMON_PORT}")
    output.info("In VS Code, on the open file: 'ibmicli: debug batch' or "
                "'ibmicli: service entry point'.")
    return 0


def _host(cfg: Config) -> str:
    """Validated Debug Service hostname; reject missing values and IP addresses."""
    where = (
        f"       set it in {cfg.path} at the top of the file, before any\n"
        "       [table] (after a table it would belong to that table):\n"
        "         debug_host = \"ibmi.example.com\""
    )
    why = (
        "       use a hostname covered by the Debug Service certificate, not\n"
        "       an IP address: when a service entry point is hit, the IBM extension\n"
        "       verifies the certificate and the IP connection fails\n"
    )
    if not cfg.debug_host:
        raise ConfigError(
            "missing 'debug_host': the hostname VS Code uses for the Debug Service\n"
            + why + where
        )
    if not is_ip(cfg.debug_host):
        return cfg.debug_host
    raise ConfigError(f"'debug_host' is an IP address ({cfg.debug_host})\n" + why + where)


def is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


# check_host outcomes.
HOST_OK, HOST_CERTIFICATE, HOST_UNREACHABLE = "ok", "certificate", "unreachable"


def check_host(host: str, timeout: float = 5.0) -> tuple[str, str]:
    """Probe TLS on 8005 with the same hostname verification used by IBM debug.

    Distinguish an invalid certificate from an unreachable host: the latter
    may simply mean the local network is unavailable. Return status and details.
    """
    context = ssl.create_default_context()
    try:
        with socket.create_connection((host, DEBUG_PORT), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                return HOST_OK, tls.version() or ""
    except ssl.SSLCertVerificationError as exc:
        return HOST_CERTIFICATE, exc.verify_message or str(exc)
    except (OSError, ssl.SSLError) as exc:
        return HOST_UNREACHABLE, str(exc)


def batch_problems(command: str) -> list[str]:
    """Problems in a debug_batch command; empty means valid."""
    # The Debug Service executes this directly, so it must be unprefixed CL.
    if kind_of(command) != CL:
        return [f"must be unprefixed CL (usually SBMJOB): {command}"]
    return problems(Task(name="debug_batch", steps=(command,)))


def _batch(
    cfg: Config,
    env: Env | None,
    source: Path,
    member: str,
    ext: str,
    found: prj.Target | None,
) -> str:
    """Expand the batch submission command with the same task placeholder rules."""
    command = cfg.debug_batch
    if not command:
        raise ConfigError(
            "missing 'debug_batch': the command that submits the program for\n"
            f"       debug. Set it in {cfg.path}, at the top of the file before\n"
            "       any [table], for example:\n"
            f"         debug_batch = \"{BATCH_EXAMPLE}\"\n"
            "       uses task placeholders: ibmicli tasks --guide"
        )
    trouble = batch_problems(command)
    if trouble:
        raise ConfigError("invalid 'debug_batch':\n       " + "\n       ".join(trouble))
    values = run_cmd._values(cfg, env, str(source), member, ext, found)
    return run_cmd._expand(Task(name="debug_batch", steps=(command,)), command, values)
