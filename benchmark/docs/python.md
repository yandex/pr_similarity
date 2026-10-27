[PR Similarity](../../README.md) / [benchmark](../README.md)

# Python inside an attempt

The Python 3.12 selected by `make setup` supplies both the benchmark interpreter
and the base interpreter for agents. Setup verifies its standard-library and
shared-library dependencies. There is no separate Python downloader or implicit
fallback to the system `python3`. Install a working Python 3.12 with `venv`,
`ensurepip` and `ldd` support before setup.

The runner keeps a content-verified, package-free snapshot of that base Python
in `provisioning.cache_dir`. It mounts the snapshot read-only and creates a fresh
writable `/sandbox-home/.venv` **before every attempt's setup**. Setup, diagnostics
and the agent share that attempt environment. Benchmark packages, host packages
and packages from other attempts are not inherited. A retry or a new attempt
on resume starts with a new venv; finished and cancelled attempts discard it.

Install task dependencies in setup with `python -m pip install ...`. A new venv
includes pip but no project dependencies. `python`, `python3`, `python3.12`,
standard `/usr/bin` and `/bin` Python paths, and their shebangs select this venv.
Other standard `python3.X` commands fail with an explanation. Standard pip
entrypoints use the same environment. Utilities that require distribution Python
packages may be incompatible; install the tools you need in setup.

The actual Python version and content identity are saved in `_meta/python.json`;
each attempt records its pre-setup and pre-agent checks in `python.json`.
Preparation logs survive removal of private state. Python checks fail before
model execution. Resume requires the same base Python build, including stdlib;
a changed installation requires restoring that build or starting a new run.
Patch versions may differ between separate runs. Host-only recovery, rescore
and reprice do not create an agent venv.

System libraries and non-Python tools still come from the host. This is a
consistent baseline environment, not a complete container image or a barrier
against deliberately locating nonstandard interpreter installations.
Non-Python executables retain their standard paths and symlinks so compiler
drivers such as GCC/G++ can locate their helpers and libraries.

The runner copies `/usr/bin`, `/usr/local/bin` and a separate `/bin`, when present,
into invocation-private temporary directories. Regular files retain their
permissions and timestamps; non-Python symlinks retain their targets. With
`/bin` linked to `/usr/bin`, both paths use the same copy. The copied directories
are mounted read-only at their standard paths, with Python/pip entrypoints
selecting the current attempt's venv.

The command directories are shared by attempts in one invocation. Each attempt
has its own writable HOME and venv, so installing packages in one attempt does
not change another. Resume prepares new command directories and fresh attempt
venvs. The command copies are separate from the frozen Python cache and
publication artifacts.

Allow temporary disk space for the copied command directories and additional
startup time. Their size depends on the host's installed tools; independent
benchmark invocations each allocate their own copies. System package updates
during an active invocation are unsupported: executables remain copies while
system libraries are mounted directly from the host.

Normal shutdown, including handled cancellation, removes the temporary copies.
An uncatchable termination such as SIGKILL can leave them for host temporary-file
cleanup. Agent execution supports bubblewrap 0.8.0 with working unprivileged
user namespaces.
