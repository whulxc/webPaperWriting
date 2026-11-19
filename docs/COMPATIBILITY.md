# V0.8.1 compatibility and validation

The release label is **V0.8.1**. The Python package version is `0.8.1`. The shared writing skill has its own version, recorded in its manifest. The following matrix describes intended deployment routes and the implementation boundaries visible in this release.

| Host | Native tooling target | Live project execution | Controlled build worker | Real-machine release acceptance |
| --- | --- | --- | --- | --- |
| Linux | CLI, bootstrap, handoff, skill packaging and stdio Gateway | POSIX host with its own registered WebCodex projects | Linux isolation with the listed prerequisites | Not performed |
| WSL 2 | Linux tooling within one chosen distribution | WSL-hosted Gateway and projects, configured using Linux paths | WSL 2 with Linux namespaces, user systemd and cgroup limits | Not performed |
| Windows | Native bootstrap, handoff, text-review source preparation, skill packaging and catalog import | Run the execution Gateway in WSL 2 or on Linux | WSL 2 or Linux execution host | Not performed |
| macOS | Portable CLI, skill tooling and POSIX Gateway configuration | POSIX Gateway, subject to required project/service configuration | Linux execution host | Not performed |

The Python package is distributed as a platform-independent wheel. That packaging tag describes its Python source and bundled resources. The worker still requires a compatible operating system and external software.

## Verification performed for this release

Release preparation uses static source validation, package-content checks and synthetic protocol or filesystem fixtures. A synthetic environment without `fcntl` verifies that public Gateway catalog imports succeed and protected filesystem operations fail before creating state. A synthetic macOS platform value verifies that the Linux sandbox refuses a build before touching the output. These checks establish code-path behavior only.

Native installation, desktop integration, service startup, reboot persistence, real account connections, actual paper compilation and live cloud publication have not been exercised as V0.8.1 release acceptance. The same status applies to all four target platforms. CI results, when present, identify their own operating system and fixture coverage separately.

## Runtime prerequisites

- Python 3.11 or newer and the package's declared Python dependencies.
- POSIX filesystem primitives for protected Gateway operations, including `fcntl`, descriptor-relative traversal and `O_NOFOLLOW`.
- A separately installed compatible WebCodex runtime and explicit user-owned project registrations for live native tool execution.
- Linux namespaces, `bubblewrap`, user-level systemd/cgroups and a complete configured Python prefix for controlled workers.
- The current worker's TeX Live `x86_64-linux` binary layout. ARM hosts require a compatible Linux build host for this worker.

The code rejects link/reparse components in configured roots. Unsupported protected filesystem and worker operations fail with a platform or prerequisite error. Private credentials and mutable state must reside on a host filesystem providing the security semantics expected by the corresponding backend.

## Optional backends

| Capability | Dependency or condition |
| --- | --- |
| TeX compilation | The configured trusted TeX targets, TeX Live, matching bibliography engine and fonts on the Linux worker |
| PDF page review | A configured supported renderer and its separate license terms |
| Reference management | A project-scoped Zotero backend or supplied bibliography files |
| Public metadata search | Explicitly enabled public provider and request budget |
| Drive publication and comments | User-authorized credentials, project folder and exact file/build identity |
| PowerPoint automation | Separately configured Windows PowerPoint host |

Optional dependencies and notices are recorded in `NOTICE`. Package releases, skill updates, runtime compatibility and author acceptance are tracked independently.
