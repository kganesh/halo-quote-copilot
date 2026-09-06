"""Stage-aware readiness: what is running, and whether this stage allows it.

The tests worth the most here are the ones asserting a probe reports a
*substitute* rather than a pass. Every optional dependency in this codebase
falls back silently and correctly, which is the property that makes a laptop
pleasant and a production deployment a lie. A probe that returned `ok` for the
local guardrail would reproduce exactly the problem it exists to solve.
"""

import pytest

from halo import readiness
from halo.readiness import Stage, State


@pytest.fixture(autouse=True)
def _no_inherited_configuration(monkeypatch):
    """Start every test from an unconfigured environment.

    Otherwise these pass or fail depending on what the shell that ran pytest
    happened to export, which is the least useful kind of flake."""
    for name in (
        "HALO_GUARDRAIL_ID",
        "HALO_GUARDRAIL_VERSION",
        "HALO_EVENTS_BUCKET",
        "HALO_COGNITO_ISSUER",
        "HALO_COGNITO_AUDIENCE",
        "HALO_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


def result(name: str, assessments) -> readiness.ProbeResult:
    return next(a.result for a in assessments if a.result.name == name)


class TestProbesReportWhatIsRunning:
    def test_an_unconfigured_guardrail_is_a_substitute_not_a_pass(self):
        """The whole point. `LocalGuardrail` works, so nothing errors, and a
        deployment can run regex while believing it runs the managed policy."""
        probe = readiness.probe_guardrail()

        assert probe.state is State.FALLBACK
        assert "local pattern set" in probe.detail
        assert "HALO_GUARDRAIL_ID" in probe.fix

    def test_a_configured_guardrail_reports_its_id_and_version(self, monkeypatch):
        monkeypatch.setenv("HALO_GUARDRAIL_ID", "gr-abc123")
        monkeypatch.setenv("HALO_GUARDRAIL_VERSION", "3")
        probe = readiness.probe_guardrail()

        assert probe.state is State.OK
        assert "gr-abc123" in probe.detail
        assert "(3)" in probe.detail

    def test_an_unconfigured_event_sink_names_the_file_it_is_using(self):
        probe = readiness.probe_events()
        assert probe.state is State.FALLBACK
        assert "one copy, on this disk" in probe.detail

    def test_half_configured_identity_is_missing_not_a_substitute(self, monkeypatch):
        """Issuer without audience is not a working fallback, it is a mistake.
        Reporting it as a substitute would file it under "fine on a laptop"."""
        monkeypatch.setenv("HALO_COGNITO_ISSUER", "https://cognito-idp.us-east-1.amazonaws.com/x")
        probe = readiness.probe_identity()

        assert probe.state is State.MISSING
        assert "checked together or not at all" in probe.detail

    def test_an_unpriced_model_is_reported_as_missing(self, monkeypatch):
        """A dollar budget that cannot stop a run is not a working substitute."""
        monkeypatch.setenv("HALO_MODEL", "us.anthropic.claude-nonesuch-9")
        probe = readiness.probe_model_priced()

        assert probe.state is State.MISSING
        assert "max_usd cannot stop a run" in probe.detail

    def test_checkpoints_are_honest_about_not_being_durable(self):
        """There is no durable implementation yet. A probe tuned to pass anyway
        would be a check written to give the answer we wanted."""
        assert readiness.probe_checkpoints().state is State.FALLBACK


class TestTheStageDecides:
    def test_local_allows_every_substitute(self):
        assessments, ok = readiness.run(Stage.LOCAL)

        assert ok
        assert any(a.result.state is State.FALLBACK for a in assessments)

    def test_integration_refuses_a_local_guardrail_and_a_local_sink(self):
        _, ok = readiness.run(Stage.INTEGRATION)
        assert not ok

        failed = {a.result.name for a in readiness.run(Stage.INTEGRATION)[0] if not a.passed}
        assert failed == {"guardrail", "events"}

    def test_integration_still_tolerates_development_identity(self):
        """The environment exists to exercise the managed services, not to hold
        a real user directory."""
        assessments, _ = readiness.run(Stage.INTEGRATION)
        identity = next(a for a in assessments if a.result.name == "identity")

        assert identity.result.state is State.FALLBACK
        assert identity.passed

    def test_production_adds_identity_and_durable_state(self, monkeypatch):
        monkeypatch.setenv("HALO_GUARDRAIL_ID", "gr-abc123")
        monkeypatch.setenv("HALO_EVENTS_BUCKET", "halo-evidence")
        failed = {a.result.name for a in readiness.run(Stage.PRODUCTION)[0] if not a.passed}

        assert failed == {"identity", "checkpoints"}

    def test_a_fully_configured_deployment_is_not_yet_production_ready(self, monkeypatch):
        """Honest result, not a discouraging one: durable checkpoints do not
        exist, so no amount of environment configuration reaches production."""
        for name, value in {
            "HALO_GUARDRAIL_ID": "gr-abc123",
            "HALO_EVENTS_BUCKET": "halo-evidence",
            "HALO_COGNITO_ISSUER": "https://cognito-idp.us-east-1.amazonaws.com/pool",
            "HALO_COGNITO_AUDIENCE": "client-id",
        }.items():
            monkeypatch.setenv(name, value)

        assessments, ok = readiness.run(Stage.PRODUCTION)

        assert not ok
        assert {a.result.name for a in assessments if not a.passed} == {"checkpoints"}

    def test_every_probe_runs_at_every_stage(self):
        """`doctor` stops at the first failure because it is a dependency chain.
        This is a list of what to fix, and stopping early would hide the second
        thing from whoever is fixing the first."""
        for stage in Stage:
            assessments, _ = readiness.run(stage)
            assert len(assessments) == len(readiness.PROBES)


class TestTheContract:
    def test_the_strict_sets_only_widen(self):
        """local ⊆ integration ⊆ production. A probe strict locally and lax in
        production would be a table nobody could reason about."""
        for probe in readiness.PROBES:
            strict = probe.strict_from
            if Stage.LOCAL in strict:
                assert Stage.INTEGRATION in strict and Stage.PRODUCTION in strict, probe.name
            if Stage.INTEGRATION in strict:
                assert Stage.PRODUCTION in strict, probe.name

    def test_every_probe_that_can_fail_offers_a_fix(self):
        for probe in readiness.PROBES:
            found = probe.run()
            if found.state is not State.OK:
                assert found.fix, f"{probe.name} degrades with no way out"

    def test_nothing_here_calls_aws(self):
        """It runs on every deploy and can serve a readiness endpoint, so it has
        to be free. Cost Explorer is the pointed omission: that API bills $0.01
        a request, which is a strange thing to pay on a health check."""
        import inspect

        source = inspect.getsource(readiness)
        for forbidden in ("boto3", "invoke_model", "get_cost_and_usage", "apply_guardrail"):
            assert forbidden not in source, f"readiness must not touch {forbidden}"


class TestTheCommand:
    def test_local_exits_zero(self, capsys):
        from halo import cli

        assert cli.main(["ready"]) == 0
        assert "Ready for local." in capsys.readouterr().out

    def test_production_exits_two_and_names_what_is_missing(self, capsys):
        from halo import cli

        assert cli.main(["ready", "--stage", "production"]) == 2
        printed = capsys.readouterr().out
        assert "Not ready for production" in printed
        assert "checkpoints" in printed

    def test_the_local_report_says_what_it_is_substituting(self, capsys):
        """Passing quietly would leave the same blind spot in a different
        place: you would know it was allowed, not what it was."""
        from halo import cli

        cli.main(["ready"])
        assert "Running on substitutes" in capsys.readouterr().out
