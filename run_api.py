"""Convenience entry point for the dashboard API — `python run_api.py`.

Two processes on purpose. This one is a supervisor: it watches the source tree, owns the
"only one instance" claim, and replaces the serving process *whole* on every change. The child
does nothing but serve.

Runs uvicorn under a **watchfiles supervisor** rather than uvicorn's own `--reload`, because
uvicorn's reloader cannot restart this app on Windows. Its `restart()`
(uvicorn/supervisors/basereload.py) sends `os.kill(pid, CTRL_C_EVENT)` and then
`self.process.join()` with no timeout. `CTRL_C_EVENT` is only delivered to a process attached to
a console process group, so when the server is started without a real console — a background
runner, or stdout redirected to a file — the worker never receives it and the join blocks for
ever. The reloader logs "Reloading...", wedges, and the *old* worker keeps serving. Observed
here for over an hour: every edit was on disk, none of them were being served, and each
"Reloading..." was followed by a log line with a leading space — uvicorn's own
`sys.stdout.write(" ")` workaround, which is the tell.

watchfiles stops its child with `os.kill(pid, SIGINT)`, which on Windows maps to
`TerminateProcess` — a real kill that needs no console and cannot wedge. The cost is that the
child dies hard, so the FastAPI lifespan's `finally` does not run; see `_serve`.

Module-level imports are stdlib only, deliberately. `multiprocessing`'s spawn re-executes this
whole file via `runpy.run_path` in the child on every restart, so anything imported out here is
imported again on every save.
"""
import os
import socket
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

WATCH_DIRS = ("api", "config", "connectors", "operations", "pipeline")
"""The directories whose code the server actually imports.

An allow-list, not a deny-list, and the difference is the point. `state/` holds three SQLite
files whose `-wal`/`-shm` sidecars are rewritten every fifteen seconds by the arrivals poller,
plus an attachments tree that grows on every run; watching the repo root and filtering those out
would leave a restart loop one filter bug away. Naming the directories instead means `state/` is
never registered with the OS watcher at all.

`connectors/` belongs here and is easy to miss — it holds no routes, but `pipeline/po_verify.py`,
`pipeline/stage1_ingest.py` and `pipeline/ingest_orchestrator.py` all import from it, so it is
served code.

Deliberately absent:
  * `tools/`   — `operations.runner` imports `tools.ingest_mailbox` lazily and Python caches it
                 in `sys.modules` for the life of the process, so an edit there was never picked
                 up mid-process anyway; it only ever produced a spurious restart. Edit and
                 restart by hand.
  * `tests/`, `docs/`, `sample_data/`, `state/`, `.venv/`, `run_pipeline.py`.
  * `run_api.py` itself — restarting the child would leave *this* process running the old
    supervisor code, the most confusing possible half-state.
"""


def _watch_paths() -> list[Path]:
    paths = [BASE_DIR / name for name in WATCH_DIRS]

    # The one file outside those directories worth restarting for. `config/settings.py` calls
    # load_dotenv() at import, so an edited value — a refreshed SPITFIRE_SESSION_COOKIE, most
    # often — reaches the app on a whole-process restart and by no other route. Named as a file
    # rather than watching BASE_DIR, which would drag in state/ and .venv/. Verified that the
    # watch really is scoped to the file: touching tools/*.py and state/console.sqlite3 while
    # watching only .env produces no events.
    env = BASE_DIR / ".env"
    if env.exists():
        paths.append(env)
    return paths


def _serve() -> None:
    """The child: one plain uvicorn, no reloader of its own.

    Reached from the spawned child by pickled reference. Restarting is the supervisor's job —
    `reload=True` here would nest uvicorn's broken reloader inside a working one, and worse:
    watchfiles would kill the nested *supervisor* and orphan the innermost worker, which keeps
    the listening socket on a port this process can no longer see.

    This process is hard-killed on restart, so `api.main`'s lifespan `finally` (which calls
    `scheduler.stop()`) does not run. Nothing is lost by that: the scheduler thread is a daemon,
    the kill switch is persisted and re-loaded on boot, and SQLite's WAL is built to survive
    exactly this. The one rough edge is a run killed mid-pass, which leaves its `runs` row with a
    NULL `finished_at` and so reads as permanently in progress on /ui/automation — so don't save
    a file while Automation says a run is going.
    """
    # Before anything imports `config.settings`. The supervisor above us already ran
    # `load_dotenv()` when it imported `api.config` for the port, which put the .env values of that
    # moment into *its* environment — and a spawned child inherits them. `load_dotenv` does not
    # overwrite a variable that is already set, so without this the child's own load is a no-op
    # against a stale inherited value: editing .env restarted the server and changed nothing.
    #
    # Measured on 2026-08-17 with SPITFIRE_SESSION_COOKIE — a fresh cookie in .env, a clean restart
    # logged, and every live read still failing on the old one. That is worse than not watching
    # .env at all, because the restart makes it look as though the new value took effect.
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env", override=True)

    import uvicorn

    from api import config

    print(f"[run_api] worker pid {os.getpid()} serving "
          f"http://{config.API_HOST}:{config.API_PORT}", flush=True)
    uvicorn.run("api.main:app", host=config.API_HOST, port=config.API_PORT, reload=False)


def _something_is_listening(host: str, port: int) -> bool:
    """A *connect* probe, deliberately — not a bind probe.

    `bind()` without SO_REUSEADDR reports WSAEADDRINUSE on Windows when the port merely holds
    connections in TIME_WAIT, which is exactly what a server stopped five seconds ago leaves
    behind (every browser keep-alive to 127.0.0.1:8000 lands there). That false positive would
    fire on almost every restart by hand. A connect only succeeds against something actually
    accepting.
    """
    probe = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    with socket.socket() as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((probe, port)) == 0


def _on_change(changes) -> None:
    names = sorted({os.path.relpath(path, BASE_DIR) for _change, path in changes})
    shown = ", ".join(names[:5]) + (f" (+{len(names) - 5} more)" if len(names) > 5 else "")
    print(f"[run_api] change: {shown} -> restarting", flush=True)


if __name__ == "__main__":
    # Everything below runs in the SUPERVISOR ONLY. The child re-executes this file under the
    # name "__mp_main__", so this block is skipped there — which is what keeps the
    # single-instance check off the respawn path, where the port is legitimately occupied by the
    # very server being restarted.
    from watchfiles import PythonFilter, run_process

    from api import config

    if _something_is_listening(config.API_HOST, config.API_PORT):
        sys.exit(
            f"\n[run_api] REFUSING TO START - something is already serving "
            f"{config.API_HOST}:{config.API_PORT}.\n"
            f"          Two instances share state/*.sqlite3 and would both poll the mailbox.\n"
            f"          Find and stop it first:\n"
            f"              Get-NetTCPConnection -LocalPort {config.API_PORT} -State Listen | "
            f"Select-Object OwningProcess\n"
        )

    paths = _watch_paths()
    print(f"[run_api] supervisor pid {os.getpid()}", flush=True)
    # ASCII only in the banner: this goes to whatever console started the server, and on a
    # cp1252 Windows terminal an em dash arrives as a replacement character.
    print(f"[run_api] restarting on *.py changes in: {', '.join(WATCH_DIRS)}, and .env",
          flush=True)
    print("[run_api] NOT watched: state/, tools/, tests/, docs/, .venv/, run_api.py", flush=True)

    # PythonFilter, not the default: DefaultFilter does not exclude `.sqlite3` or its `-wal`
    # sidecar, so the poller's own writes would restart the server. It matches on suffix, which
    # is why ".env" works here despite `.env` having no extension in the splitext sense. It also
    # inherits DefaultFilter's ignored directories, so the `__pycache__` writes that every
    # restart itself produces cannot cause the next one.
    reloads = run_process(
        *paths,
        target=_serve,
        watch_filter=PythonFilter(extra_extensions=(".env",)),
        callback=_on_change,
        step=150,          # group an editor's "save all" into one restart, not three
        debounce=400,
    )
    print(f"[run_api] stopped after {reloads} restart(s).", flush=True)
