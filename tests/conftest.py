"""Shared pytest configuration.

Hypothesis is registered here in a deliberately *derandomized* profile: the
property tests in test_web_jobs_properties.py exist to pin invariants, and a
suite that fails only sometimes teaches nobody anything. With
``derandomize=True`` every run draws the same examples, so a failure is
reproducible from the test name alone and CI can't go red on a Tuesday for
reasons nobody can reproduce on Wednesday.

The deadline is disabled for the same reason: these tests build JobStores
(which touch the filesystem and start a worker thread), so per-example
timings vary far too much for a wall-clock deadline to mean anything.
"""

from __future__ import annotations

from hypothesis import HealthCheck, settings

settings.register_profile(
    "stemlab",
    # A few hundred examples per property: enough to reach the interesting
    # corners of the strategies below (surrogates, size boundaries, every
    # status) while keeping the whole suite comfortably inside a couple of
    # minutes.
    max_examples=200,
    derandomize=True,
    deadline=None,
    # Several properties build a JobStore per example, which is genuinely
    # slow-ish; that's inherent to what's being tested, not a sign the
    # strategy is misbehaving.
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
settings.load_profile("stemlab")
