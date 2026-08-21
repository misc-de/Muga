"""Tests for the call tracer and its main-loop stall watchdog.

This is debugging infrastructure — it only runs under --trace — but it is the
tool you reach for when the UI freezes and you cannot reproduce it, so it has
to work the first time. Two things in particular:

  * the profile hook runs on *every* Python call in the process, so a mistake
    there costs a multiple of normal runtime, and an exception there would
    surface in whatever unrelated code happened to be running.
  * the watchdog decides "the main loop is stuck" from a heartbeat. Its
    thresholds are what separate a useful dump from a log full of false
    alarms during startup, when no main loop is running yet.
"""

from __future__ import annotations

import io
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

tracer = pytest.importorskip("yaga.tracer")


@pytest.fixture(autouse=True)
def _restore_tracer_state():
    """The module keeps process-global state; tests must not leak it."""
    import sys

    saved = (tracer._trace_file, dict(tracer._last_event_time),
             dict(tracer._last_event_repr), list(tracer._main_heartbeat),
             sys.getprofile())
    yield
    tracer._trace_file = saved[0]
    tracer._last_event_time.clear()
    tracer._last_event_time.update(saved[1])
    tracer._last_event_repr.clear()
    tracer._last_event_repr.update(saved[2])
    tracer._main_heartbeat[:] = saved[3]
    sys.setprofile(saved[4])
    threading.setprofile(None)


# ---------------------------------------------------------------------------
# Argument formatting
# ---------------------------------------------------------------------------

def _args_of(*call_args, **call_kwargs):
    """Format the arguments of a call, the way the profile hook does."""
    import inspect

    captured = {}

    def sample(a, b=None, *, c=None):
        captured["text"] = tracer._format_args(inspect.currentframe())

    sample(*call_args, **call_kwargs)
    return captured["text"]


def test_arguments_are_named_and_repred() -> None:
    assert _args_of("x", 42) == "a='x', b=42, c=None"


def test_keyword_only_arguments_are_included() -> None:
    assert "c='k'" in _args_of("x", 1, c="k")


def test_a_long_argument_is_truncated() -> None:
    """A trace line per call means a repr of a 10 MB list would be written
    once per call — the log would be unusable and the disk would fill."""
    text = _args_of("y" * 5000, 1)
    assert len(text) < 200
    assert "..." in text


def test_truncation_respects_the_configured_width() -> None:
    text = _args_of("y" * 5000, 1)
    first = text.split(", ")[0]
    assert len(first) <= tracer._MAX_ARG_REPR + len("a=")


def test_an_unrepresentable_argument_does_not_break_the_hook() -> None:
    """The hook runs inside arbitrary code; raising here would surface as a
    failure in something unrelated."""

    class Hostile:
        def __repr__(self):
            raise RuntimeError("no repr for you")

    assert "<unreprable>" in _args_of(Hostile(), 1)


def test_a_frame_with_no_arguments() -> None:
    import inspect

    def sample():
        return tracer._format_args(inspect.currentframe())

    assert sample() == ""


# ---------------------------------------------------------------------------
# Which frames get logged
# ---------------------------------------------------------------------------

def test_only_yaga_frames_are_logged() -> None:
    """Logging every call in the process — gi, pytest, the stdlib — buries the
    one line that matters."""
    import inspect

    assert tracer._is_yaga_frame(inspect.currentframe()) is False   # tests/


def test_a_yaga_frame_is_recognised() -> None:
    import inspect

    from yaga import models

    frame = None

    def capture(*_a, **_k):
        nonlocal frame
        frame = inspect.currentframe().f_back

    original = models.media_type_for
    try:
        models.media_type_for = capture
        models.media_type_for(Path("x.jpg"))
    finally:
        models.media_type_for = original

    # The captured frame is this test's, so build the check from the module
    # path instead — the predicate is a substring test on the filename.
    import os

    assert tracer._is_yaga_frame(
        type("F", (), {"f_code": type("C", (), {
            "co_filename": f"{os.sep}yaga{os.sep}app.py"})()})())


# ---------------------------------------------------------------------------
# The profile hook
# ---------------------------------------------------------------------------

def _hook_frame(filename, func="do_thing"):
    code = type("Code", (), {
        "co_filename": filename, "co_name": func, "co_qualname": func,
        "co_firstlineno": 42, "co_argcount": 0, "co_kwonlyargcount": 0,
        "co_varnames": (),
    })()
    return type("Frame", (), {"f_code": code, "f_locals": {}})()


def test_the_hook_ignores_everything_but_calls() -> None:
    """It is installed process-wide; doing work on return/exception events
    would multiply the cost for no extra information."""
    tracer._trace_file = io.StringIO()
    import os

    tracer._profile(_hook_frame(f"{os.sep}yaga{os.sep}app.py"), "return", None)
    assert tracer._trace_file.getvalue() == ""


def test_the_hook_writes_a_line_for_a_yaga_call() -> None:
    import os

    tracer._trace_file = io.StringIO()
    tracer._profile(_hook_frame(f"{os.sep}yaga{os.sep}app.py", "refresh"), "call", None)
    written = tracer._trace_file.getvalue()
    assert "refresh" in written
    assert "app.py:42" in written


def test_the_hook_skips_foreign_frames_but_still_tracks_liveness() -> None:
    """The watchdog must not fire while the main thread is busy inside gi or
    Adw — those frames are not logged, but they are proof of progress."""
    tracer._trace_file = io.StringIO()
    tracer._last_event_time.clear()

    tracer._profile(_hook_frame("/usr/lib/python3/gi/overrides.py"), "call", None)

    assert tracer._trace_file.getvalue() == "", "a foreign frame was logged"
    assert tracer._last_event_time, "liveness was not recorded"


def test_the_hook_survives_a_closed_log() -> None:
    """The file is closed on shutdown while other threads may still be
    running; a raise here would take them down."""
    import os

    handle = io.StringIO()
    handle.close()
    tracer._trace_file = handle
    tracer._profile(_hook_frame(f"{os.sep}yaga{os.sep}app.py"), "call", None)


def test_the_hook_works_without_a_log_file() -> None:
    import os

    tracer._trace_file = None
    tracer._profile(_hook_frame(f"{os.sep}yaga{os.sep}app.py"), "call", None)


def test_the_hook_records_the_last_event_per_thread() -> None:
    """That string is what the stall dump reports as "last main event"."""
    import os

    tracer._trace_file = io.StringIO()
    tracer._last_event_repr.clear()
    tracer._profile(_hook_frame(f"{os.sep}yaga{os.sep}viewer.py", "show_item"),
                    "call", None)
    tid = threading.current_thread().ident
    assert "show_item" in tracer._last_event_repr[tid]


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

def test_the_heartbeat_keeps_its_timeout_alive() -> None:
    """Returning False removes the GLib source, and the watchdog would then
    report a permanent stall."""
    assert tracer._heartbeat_tick() is True


def test_the_heartbeat_advances() -> None:
    tracer._main_heartbeat[0] = 0.0
    tracer._heartbeat_tick()
    first = tracer._main_heartbeat[0]
    assert first > 0
    time.sleep(0.01)
    tracer._heartbeat_tick()
    assert tracer._main_heartbeat[0] > first


def test_starting_the_heartbeat_schedules_a_timeout() -> None:
    from gi.repository import GLib

    tracer._main_heartbeat[0] = 0.0
    with patch.object(GLib, "timeout_add") as timeout:
        tracer.start_heartbeat()
    timeout.assert_called_once()
    assert timeout.call_args[0][0] == 1000, "the heartbeat is not 1 Hz"
    assert tracer._main_heartbeat[0] > 0, "the initial beat was not seeded"


def test_thread_names_are_resolved_for_the_dump() -> None:
    current = threading.current_thread()
    assert tracer._thread_name_for(current.ident) == current.name
    assert tracer._thread_name_for(-1) == "?"


# ---------------------------------------------------------------------------
# The stall watchdog
# ---------------------------------------------------------------------------

def _run_watchdog_once(*, heartbeat, sleeps=1):
    """Run the watchdog loop for a bounded number of iterations."""
    tracer._main_heartbeat[0] = heartbeat
    calls = {"n": 0}

    def fake_sleep(_seconds):
        calls["n"] += 1
        if calls["n"] > sleeps:
            raise StopIteration

    with patch.object(tracer.time, "sleep", side_effect=fake_sleep), \
         patch.object(tracer.faulthandler, "dump_traceback") as dump:
        try:
            tracer._watchdog()
        except StopIteration:
            pass
    return dump


def test_the_watchdog_is_quiet_before_the_main_loop_starts() -> None:
    """A zero heartbeat means the loop has not run yet — dumping there would
    fire on every startup."""
    tracer._trace_file = io.StringIO()
    dump = _run_watchdog_once(heartbeat=0.0)
    dump.assert_not_called()
    assert tracer._trace_file.getvalue() == ""


def test_the_watchdog_is_quiet_while_the_loop_is_ticking() -> None:
    tracer._trace_file = io.StringIO()
    dump = _run_watchdog_once(heartbeat=time.monotonic())
    dump.assert_not_called()


def test_the_watchdog_dumps_on_a_stall() -> None:
    tracer._trace_file = io.StringIO()
    stale = time.monotonic() - (tracer._STALL_SECONDS + 1)
    dump = _run_watchdog_once(heartbeat=stale)
    dump.assert_called_once()
    written = tracer._trace_file.getvalue()
    assert "STALL" in written
    assert "thread dump" in written


def test_the_dump_names_the_last_main_event() -> None:
    """Without it the traceback shows where the thread is now, not what it was
    doing when it stopped making progress."""
    tracer._trace_file = io.StringIO()
    tracer._last_event_repr[threading.main_thread().ident] = "app.py:1 refresh()"
    _run_watchdog_once(heartbeat=time.monotonic() - (tracer._STALL_SECONDS + 1))
    assert "refresh()" in tracer._trace_file.getvalue()


def test_the_dump_covers_the_other_threads_too() -> None:
    """A UI stall is usually something a worker is holding."""
    tracer._trace_file = io.StringIO()
    tracer._last_event_repr[999999] = "scanner.py:2 scan()"
    _run_watchdog_once(heartbeat=time.monotonic() - (tracer._STALL_SECONDS + 1))
    assert "scan()" in tracer._trace_file.getvalue()


def test_one_stall_is_reported_once() -> None:
    """The heartbeat does not advance during a stall, so an unguarded loop
    would dump every couple of seconds for as long as it lasts."""
    tracer._trace_file = io.StringIO()
    stale = time.monotonic() - (tracer._STALL_SECONDS + 1)
    dump = _run_watchdog_once(heartbeat=stale, sleeps=4)
    assert dump.call_count == 1, f"dumped {dump.call_count} times for one stall"


def test_the_watchdog_needs_a_log_to_write_to() -> None:
    tracer._trace_file = None
    dump = _run_watchdog_once(heartbeat=time.monotonic() - (tracer._STALL_SECONDS + 1))
    dump.assert_not_called()


def test_the_stall_threshold_is_a_freeze_not_a_hiccup() -> None:
    """Short enough to catch a real freeze, long enough that a slow scan
    batch or a GC pause does not trip it."""
    assert 2.0 <= tracer._STALL_SECONDS <= 10.0
    assert tracer._WATCHDOG_INTERVAL < tracer._STALL_SECONDS


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------

def test_install_opens_a_log_and_hooks_the_profiler(tmp_path: Path) -> None:
    import sys

    target = tmp_path / "trace.log"
    with patch.object(tracer.threading, "Thread") as thread:
        assert tracer.install(target) == target
    try:
        assert sys.getprofile() is tracer._profile
        thread.assert_called_once()
        assert thread.call_args.kwargs["daemon"] is True, (
            "a non-daemon watchdog would keep the process alive at exit")
    finally:
        sys.setprofile(None)
        threading.setprofile(None)
        if tracer._trace_file is not None:
            tracer._trace_file.close()
            tracer._trace_file = None

    assert "Yaga trace started" in target.read_text()


def test_install_creates_the_log_directory(tmp_path: Path) -> None:
    import sys

    target = tmp_path / "deep" / "nested" / "trace.log"
    with patch.object(tracer.threading, "Thread"):
        tracer.install(target)
    try:
        assert target.exists()
    finally:
        sys.setprofile(None)
        threading.setprofile(None)
        if tracer._trace_file is not None:
            tracer._trace_file.close()
            tracer._trace_file = None


def test_install_defaults_to_the_configured_path(tmp_path: Path) -> None:
    import sys


    target = tmp_path / "default-trace.log"
    with patch.object(tracer, "TRACE_LOG_PATH", target), \
         patch.object(tracer.threading, "Thread"):
        assert tracer.install() == target
    try:
        assert target.exists()
    finally:
        sys.setprofile(None)
        threading.setprofile(None)
        if tracer._trace_file is not None:
            tracer._trace_file.close()
            tracer._trace_file = None
