# ADR 0014: Role commands run in a container, behind the existing executor seam

## Status

Accepted (2026-11-24). Opt-in: `REWORK_EXECUTOR=docker` or `auto`; `local` stays the
default.

## Context

`LocalExecutor`'s own docstring concedes the point: the path jail holds for the file
tools, but `run_command` starts a real shell as the daemon's user, and a shell can `cd`
anywhere. Two facts made that worse than it reads:

- On `rewvis` the daemon runs as `ubuntu`, who is in the `docker` group. Anything that can
  run a command can reach `/var/run/docker.sock` and start a privileged container, so a
  sandbox added above a docker-group user is theatre until the socket is out of reach.
- Before ADR 0009 the role environment was the daemon's environment, keys included. The
  allowlist closed the read channel; this ADR closes the reach channel.

The seam to build behind already exists: `ToolExecutor` is `execute` plus `specs`, and
ADR 0010 R1 already requires `LazyExecutor.attach` to take a factory rather than hard-code
`LocalExecutor`. Nothing in a role knows or cares which executor it gets.

## Decision

**A per-run container, behind `ToolExecutor`.** `nodes/docker.py` adds `DockerExecutor`.
`read_file` and `list_dir` stay on the HOST (they read the bind-mounted worktree, they
cannot change anything, they work before a container exists, and they share the one jail
implementation in `tools.read_side`). Only `run_command` runs in the container.

**Isolation.** The container is started lazily on the first command, with `--network none`,
`--cap-drop=ALL`, `--security-opt=no-new-privileges`, a pid limit, memory and CPU caps from
the same numbers `hostlimits` computes, the run's uid/gid (no root), and exactly one mount:
the run's worktree. The docker socket, the state directory and the scratch dir are never
mounted.

**Environment.** `-e NAME` per allowlisted variable, names never values, so a secret
cannot appear in the daemon's process list. The docker CLIENT gets its own minimal
environment (`DOCKER_HOST` and friends), which is a different thing from the environment
inside the container.

**Lifecycle owned by the supervisor.** `RoleBuildCtx` gains `closers`, and
`RunSupervisor` runs them when a run ends: completion, failure and cancellation all pass
through the same `finally`. Nothing owned "end of run" before this, which is why it had to
be added before a container could exist. Containers are ephemeral (`--rm`), named
deterministically from the run id so a restarted daemon recognizes its own, and swept by
`rework gc` if the daemon was killed before a closer ran.

**Refuse, never degrade.** `REWORK_EXECUTOR=docker` with no usable docker raises
`SandboxUnavailable` at startup: an owner who asked for a sandbox must not silently get the
bare shell. `auto` is the mode that falls back to local.

**Git stays on the host.** The container may read history; it never writes. The handoff
node runs on the host exactly as before, so a role cannot rewrite history, delete a branch
or push. The rule to carry into ADR 0010's worker hosts is "the machine that owns the
worktree commits it".

## Consequences

- A role command can no longer leave the worktree, read the daemon's secrets from the
  environment, or reach the docker socket, when the executor is on.
- `local` remains the default, so nothing changes for existing deployments until they opt
  in. That is deliberate: a container per run is real overhead, and a toy repo with
  `python3 -m pytest` does not need one.
- The image must carry the repo's toolchain (`git`, the runtime). `REWORK_SANDBOX_IMAGE`
  names it; the default is a slim Python image.
- **Removing the daemon user from the `docker` group is part of the same change in spirit
  and not in code**: with it in place, a role that somehow escaped still reaches the
  socket. That is an operator step, recorded in `docs/ops.md`.
- The socket problem is why the container cannot be the whole answer on a docker-group
  user; it is the reach channel, and ADR 0009 closed the read channel.

## Alternatives considered

- **Seccomp/`bubblewrap` on the host.** Cheaper, but the machine already runs docker and a
  container gives network isolation and a clean uid story for free.
- **One long-lived container per daemon.** Loses per-run isolation: two runs would share a
  filesystem view and container-layer state.
- **Run git in the container.** Natural for a harness, but it hands a role the power to
  rewrite history and push. The owner's decision was the host commits.
- **Reuse `command_prefix` inside the container.** That wrapper is a HOST resource nanny;
  the container's cgroup limits replace it, and running both would double-cap.
