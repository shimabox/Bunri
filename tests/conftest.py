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
    "bunri",
    # Enough examples to reach the interesting corners of the strategies
    # (surrogates, size boundaries, path shapes, every status) while keeping
    # the suite quick enough that people actually run it. Most of these draw
    # from sampled_from menus rather than open-ended text, so the space is
    # small and covering it does not take many hundreds of tries; the few
    # properties that want more, or that build a JobStore per example and so
    # want fewer, say so locally.
    max_examples=100,
    derandomize=True,
    deadline=None,
    # All three of these flag things that are inherent to what is being
    # tested rather than signs of a misbehaving strategy:
    #   too_slow / function_scoped_fixture -- several properties build a real
    #     JobStore per example, which is genuinely slow-ish.
    #   filter_too_much -- the round-trip properties are conditional on the
    #     loader accepting a record ("assume(job is not None)"), and the
    #     strategies deliberately generate mostly-invalid records so that
    #     P1/P4/P6 have something hostile to work with. A high filter rate is
    #     the intended shape, not a defect. The strategies still mix the
    #     derived (acceptable) values in, so the conditional properties get a
    #     healthy supply of examples -- see _job_payloads.
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
        HealthCheck.filter_too_much,
    ],
)
settings.load_profile("bunri")
