"""Benchmark harness (KICKOFF §6 principles, PR-cadence subset).

Every gate is a **same-run ratio or an operation count — never absolute
wall-clock**. Engine and floor are timed by the same helper in the same
process (floor-parity by construction). Per KICKOFF, gates start as
*warnings* and harden after 14 green nights: a breached gate warns; only
an egregious breach (3× the threshold) fails the run, so a real
regression cannot land silently while normal CI noise cannot cry wolf.

Run: ``uv run pytest benchmarks -q`` (excluded from the default test run —
``testpaths`` points at ``tests/``). Scale via ``DC_BENCH_SPECIMENS``.
"""

from __future__ import annotations

import gc
import os
import time
import warnings
from typing import Any, Callable, Iterator

import pytest

import datacrystal as dc
from benchmarks import _gen

SPECIMENS = int(os.environ.get("DC_BENCH_SPECIMENS", "60000"))
SMALL_SPECIMENS = max(1000, SPECIMENS // 10)


class BenchGateWarning(UserWarning):
    """A perf gate breached its threshold (warning stage — KICKOFF §6)."""


def time_it(fn: Callable[[], Any], *, rounds: int = 5) -> float:
    """Best-of-N seconds for one call of ``fn``, GC paused — min is the
    stablest estimator for ratio gates (noise only ever adds time)."""
    best = float("inf")
    was_enabled = gc.isenabled()
    gc.collect()
    gc.disable()
    try:
        for _ in range(rounds):
            t0 = time.perf_counter()
            fn()
            elapsed = time.perf_counter() - t0
            if elapsed < best:
                best = elapsed
    finally:
        if was_enabled:
            gc.enable()
    return best


STRICT = os.environ.get("DC_BENCH_STRICT") == "1"


def gate(name: str, value: float, threshold: float, *, unit: str = "×") -> None:
    """KICKOFF §6 discipline: thresholds are the ratified targets; breaches
    WARN until the gate hardens (14 green nights), then ``DC_BENCH_STRICT=1``
    turns them into failures. Threshold changes require a PR touching both
    the threshold and the KICKOFF table row it cites (fitness rule)."""
    verdict = "OK" if value <= threshold else "BREACH"
    print(f"[gate] {name}: {value:.2f}{unit} (≤ {threshold}{unit}) {verdict}")
    if value > threshold:
        if STRICT:
            pytest.fail(
                f"perf gate {name} breached: {value:.2f}{unit} vs "
                f"threshold {threshold}{unit} (hardened via DC_BENCH_STRICT)"
            )
        warnings.warn(BenchGateWarning(
            f"perf gate {name}: {value:.2f}{unit} exceeds {threshold}{unit} "
            "(warning stage — hardens after 14 green nights, KICKOFF §6)"
        ), stacklevel=2)


@pytest.fixture(scope="session")
def big_store(tmp_path_factory) -> Iterator[dc.Store]:
    """The shared @{SPECIMENS} cabinet (sqlite, built once per session)."""
    path = tmp_path_factory.mktemp("bench") / "big.store"
    store = dc.Store.open(path)
    counts = _gen.build(store, specimens=SPECIMENS)
    print(f"[corpus] big: {counts}")
    yield store
    store.close()


@pytest.fixture(scope="session")
def small_store(tmp_path_factory) -> Iterator[dc.Store]:
    """The same shape at a tenth the extent (scale-ratio gates)."""
    path = tmp_path_factory.mktemp("bench") / "small.store"
    store = dc.Store.open(path)
    counts = _gen.build(store, specimens=SMALL_SPECIMENS)
    print(f"[corpus] small: {counts}")
    yield store
    store.close()
