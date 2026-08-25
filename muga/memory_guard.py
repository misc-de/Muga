"""System-memory pressure probe for the camera pipelines.

The pipelines in this app run on phones where two or three GB of RAM are
shared with the compositor, and GStreamer buffer pools — droidcamsrc's in
particular — can grow far faster than any queue limit bounds them. When the
kernel's OOM killer steps in on a phosh session it does not necessarily pick
us: the compositor is the bigger target, so the whole session goes down.

So the risky pipelines sample /proc while they run and shut themselves down
while there is still headroom. Everything here is best-effort — on a system
without a readable /proc the probes return None and callers just lose the
safety net rather than failing.
"""
from __future__ import annotations

import logging
from pathlib import Path

LOGGER = logging.getLogger(__name__)

_MEMINFO = Path("/proc/meminfo")
_SELF_STATUS = Path("/proc/self/status")

# Floor for "memory left on the system". The target devices span 2–8 GB, so a
# flat number is either meaningless on the small ones or far too late on the
# big ones — take whichever of the two is larger.
_MIN_AVAILABLE_KB = 150_000
_MIN_AVAILABLE_FRACTION = 0.05

# Growth of our own RSS across a single scan. A capped camera pipeline costs a
# few tens of MB; +400 MB means a buffer pool is running away and no amount of
# waiting will bring it back.
_MAX_RSS_GROWTH_KB = 400_000


def _proc_values(path: Path, keys: tuple[str, ...]) -> dict[str, int]:
    """Read the named `Key:   1234 kB` lines out of a /proc file."""
    try:
        text = path.read_text()
    except OSError:
        LOGGER.debug("could not read %s", path, exc_info=True)
        return {}
    out: dict[str, int] = {}
    for line in text.splitlines():
        name, sep, rest = line.partition(":")
        if not sep or name not in keys:
            continue
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[name] = int(parts[0])
        if len(out) == len(keys):
            break
    return out


def available_kb() -> int | None:
    """Memory the kernel thinks is available for new allocations, kB."""
    return _proc_values(_MEMINFO, ("MemAvailable",)).get("MemAvailable")


def total_kb() -> int | None:
    return _proc_values(_MEMINFO, ("MemTotal",)).get("MemTotal")


def self_rss_kb() -> int | None:
    """This process's resident set size, kB."""
    return _proc_values(_SELF_STATUS, ("VmRSS",)).get("VmRSS")


def min_available_kb() -> int:
    total = total_kb()
    if not total:
        return _MIN_AVAILABLE_KB
    return max(_MIN_AVAILABLE_KB, int(total * _MIN_AVAILABLE_FRACTION))


def pressure_reason(baseline_rss_kb: int | None = None) -> str | None:
    """A short human-readable reason to stop, or None while memory is fine.

    Pass the RSS sampled when the pipeline started to also catch a pool that
    is running away inside our own process while the system at large still
    looks healthy (the kernel reclaims caches for a while before MemAvailable
    moves, by which point we are seconds from the OOM killer).
    """
    available = available_kb()
    if available is not None and available < min_available_kb():
        return f"low system memory ({available // 1024} MB free)"
    if baseline_rss_kb is not None:
        rss = self_rss_kb()
        if rss is not None and rss - baseline_rss_kb > _MAX_RSS_GROWTH_KB:
            return f"runaway memory use (+{(rss - baseline_rss_kb) // 1024} MB)"
    return None
