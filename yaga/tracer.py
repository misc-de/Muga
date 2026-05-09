"""Call tracer + main-thread stall watchdog for debugging UI freezes.

Activated via the ``--trace`` CLI flag. When installed, every Python call
inside the ``yaga`` package is logged (function name, file:line, repr'd args,
thread, timestamp) to a dedicated trace log. A background watchdog detects
when the main thread has not made progress for a few seconds and dumps the
stack of every thread to the same log so you can see what blocks where.
"""
from __future__ import annotations

import faulthandler
import os
import sys
import threading
import time
from pathlib import Path
from typing import TextIO

from .config import TRACE_LOG_PATH

_trace_file: TextIO | None = None
_last_event_time: dict[int, float] = {}
_last_event_repr: dict[int, str] = {}
_dump_lock = threading.Lock()
_STALL_SECONDS = 5.0
_WATCHDOG_INTERVAL = 2.0
_MAX_ARG_REPR = 80


def _format_args(frame) -> str:
    code = frame.f_code
    argcount = code.co_argcount + code.co_kwonlyargcount
    names = code.co_varnames[:argcount]
    parts: list[str] = []
    for name in names:
        try:
            value = frame.f_locals.get(name, "<missing>")
            r = repr(value)
        except Exception:
            r = "<unreprable>"
        if len(r) > _MAX_ARG_REPR:
            r = r[: _MAX_ARG_REPR - 3] + "..."
        parts.append(f"{name}={r}")
    return ", ".join(parts)


def _is_yaga_frame(frame) -> bool:
    fname = frame.f_code.co_filename
    return f"{os.sep}yaga{os.sep}" in fname


def _profile(frame, event, _arg):
    if event != "call":
        return
    if not _is_yaga_frame(frame):
        return
    code = frame.f_code
    func = getattr(code, "co_qualname", code.co_name)
    short_file = os.path.basename(code.co_filename)
    line = code.co_firstlineno
    args = _format_args(frame)
    thread = threading.current_thread()
    tid = thread.ident or 0
    msg = (
        f"{time.time():.4f} [{thread.name}] "
        f"{short_file}:{line} {func}({args})"
    )
    if _trace_file is not None:
        try:
            _trace_file.write(msg + "\n")
        except Exception:
            pass
    _last_event_time[tid] = time.monotonic()
    _last_event_repr[tid] = msg


def _watchdog() -> None:
    main_tid = threading.main_thread().ident or 0
    last_dumped_at: float = 0.0
    while True:
        time.sleep(_WATCHDOG_INTERVAL)
        last = _last_event_time.get(main_tid)
        if last is None:
            continue
        idle = time.monotonic() - last
        if idle < _STALL_SECONDS:
            continue
        # Avoid re-dumping until the main thread has moved (or another stall begins).
        if last_dumped_at == last:
            continue
        last_dumped_at = last
        with _dump_lock:
            if _trace_file is None:
                continue
            try:
                _trace_file.write(
                    f"\n=== STALL: main thread idle {idle:.1f}s "
                    f"at {time.time():.4f} ===\n"
                )
                _trace_file.write(
                    f"  last main event: {_last_event_repr.get(main_tid, '<unknown>')}\n"
                )
                for tid, repr_ in list(_last_event_repr.items()):
                    if tid == main_tid:
                        continue
                    _trace_file.write(f"  last event tid={tid}: {repr_}\n")
                _trace_file.write("--- thread dump ---\n")
                _trace_file.flush()
                faulthandler.dump_traceback(file=_trace_file, all_threads=True)
                _trace_file.write("--- /thread dump ---\n\n")
                _trace_file.flush()
            except Exception:
                pass


def install(path: Path | None = None) -> Path:
    """Open the trace log, install the profile hook, start the watchdog."""
    global _trace_file
    target = Path(path) if path else TRACE_LOG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    _trace_file = open(target, "w", buffering=1, encoding="utf-8")
    _trace_file.write(
        f"=== Yaga trace started {time.time():.4f} pid={os.getpid()} ===\n"
    )
    _trace_file.flush()
    sys.setprofile(_profile)
    threading.setprofile(_profile)
    threading.Thread(
        target=_watchdog, name="yaga-trace-watchdog", daemon=True
    ).start()
    print(f"[yaga] tracing enabled -> {target}", file=sys.stderr)
    return target
