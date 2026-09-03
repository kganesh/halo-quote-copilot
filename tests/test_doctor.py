"""The preflight must stop at the first problem and name the step that fixes
it."""

from halo.doctor import Check, render, run


def test_checks_stop_at_the_first_failure(monkeypatch):
    """A later check that needs credentials must not run without them."""
    called: list[str] = []

    def failing_credentials():
        called.append("credentials")
        return Check("credentials", False, "no credentials", "aws-setup.md step 1")

    def should_not_run(*_args):
        called.append("invoke")
        raise AssertionError("ran a check that depends on an earlier failure")

    monkeypatch.setattr("halo.doctor.check_credentials", failing_credentials)
    monkeypatch.setattr("halo.doctor.check_invoke", should_not_run)

    checks, ok = run("us-east-1", "anthropic.claude-sonnet-5")

    assert not ok
    assert called == ["credentials"]
    assert len(checks) == 1


def test_every_failure_names_the_step_that_fixes_it(monkeypatch):
    monkeypatch.setattr(
        "halo.doctor.check_credentials",
        lambda: Check("credentials", False, "no credentials", "aws-setup.md step 1"),
    )
    checks, ok = run("us-east-1", "anthropic.claude-sonnet-5")
    out = render(checks, ok, region="us-east-1", model="anthropic.claude-sonnet-5")

    assert "[FAIL] credentials" in out
    assert "-> aws-setup.md step 1" in out
    assert "1 of 4 checks run" in out


def test_a_clean_run_says_what_to_do_next(monkeypatch):
    for name in ("check_credentials",):
        monkeypatch.setattr(
            f"halo.doctor.{name}", lambda: Check("credentials", True, "arn:aws:...")
        )
    monkeypatch.setattr("halo.doctor.check_region", lambda r: Check("region", True, r))
    monkeypatch.setattr(
        "halo.doctor.check_model_available", lambda r, m: Check("model access", True, "listed")
    )
    monkeypatch.setattr(
        "halo.doctor.check_invoke",
        lambda r, m: Check("invoke", True, "12 in + 5 out tokens, $0.000074 estimated"),
    )

    checks, ok = run("us-east-1", "anthropic.claude-sonnet-5")
    out = render(checks, ok, region="us-east-1", model="anthropic.claude-sonnet-5")

    assert ok
    assert out.count("[PASS]") == 4
    assert "halo quote" in out


def test_a_missing_region_is_caught_before_any_call():
    assert not check_region_result("")


def check_region_result(region: str) -> bool:
    from halo.doctor import check_region

    return check_region(region).passed


def test_listing_failures_do_not_read_as_missing_model(monkeypatch):
    """A missing ListFoundationModels permission is not the same as no model
    access."""
    import botocore.exceptions

    class Boom:
        def list_foundation_models(self):
            raise botocore.exceptions.ClientError({"Error": {"Code": "AccessDenied"}}, "List")

    monkeypatch.setattr("boto3.client", lambda *a, **k: Boom())
    from halo.doctor import check_model_available

    result = check_model_available("us-east-1", "anthropic.claude-sonnet-5")
    assert result.passed
    assert "not checked" in result.detail
