"""M8: the stack tears down to nothing, checked rather than claimed.

Most of these tests are about the check itself. A teardown gate that passes
because it parsed nothing is worse than no gate, so the first thing asserted is
that it finds the real resources, and the second is that it fails on a stack
built to fail.
"""

from datetime import date

import pytest

from halo import infra
from halo.evals import gates


@pytest.fixture
def bad_stack(tmp_path):
    (tmp_path / "bad.tf").write_text(
        """
        resource "aws_nat_gateway" "egress" {
          subnet_id = "subnet-1"
        }

        resource "aws_s3_bucket" "keepme" {
          bucket = "no-force-destroy"
          lifecycle {
            prevent_destroy = true
          }
        }

        resource "aws_cloudwatch_log_group" "forever" {
          name = "/halo/forever"
        }
        """
    )
    return tmp_path


class TestTheRealStack:
    def test_the_parser_finds_the_resources_that_are_there(self):
        """The check reporting "nothing wrong" has to mean it read something.
        The first version of this returned types with the HCL quotes still on
        them, so every lookup missed and the stack passed by finding nothing."""
        types = {resource_type for resource_type, _, _ in infra.load()}

        assert "aws_cognito_user_pool" in types
        assert "aws_bedrock_guardrail" in types
        assert "aws_s3_bucket" in types
        assert not any(name.startswith('"') for name in types)

    def test_nothing_in_it_survives_a_destroy(self):
        assert infra.check() == [], infra.report(infra.check())

    def test_the_evidence_bucket_can_be_destroyed_with_objects_in_it(self):
        """Without `force_destroy`, the teardown quietly becomes "destroy, then
        empty the bucket by hand, then destroy again"."""
        bucket = next(body for kind, _, body in infra.load() if kind == "aws_s3_bucket")
        assert bucket["force_destroy"] is True

    def test_the_log_group_expires(self):
        group = next(body for kind, _, body in infra.load() if kind == "aws_cloudwatch_log_group")
        assert group["retention_in_days"]

    def test_the_stack_has_no_vpc_and_no_compute(self):
        """The absences are the design. A VPC here would hold nothing and bill
        for it; a warm container bills continuously for a CLI that is not
        running."""
        types = {resource_type for resource_type, _, _ in infra.load()}
        assert types.isdisjoint({"aws_vpc", "aws_nat_gateway", "aws_ecs_service", "aws_lb"})


class TestTheCheckCanFail:
    def test_an_idle_biller_is_caught(self, bad_stack):
        findings = infra.check(bad_stack)
        assert any("aws_nat_gateway" in f.resource and "idle" in f.problem for f in findings)

    def test_prevent_destroy_is_caught(self, bad_stack):
        findings = infra.check(bad_stack)
        assert any("prevent_destroy" in f.problem for f in findings)

    def test_a_bucket_without_force_destroy_is_caught(self, bad_stack):
        findings = infra.check(bad_stack)
        assert any("force_destroy" in f.problem for f in findings)

    def test_a_log_group_with_no_retention_is_caught(self, bad_stack):
        findings = infra.check(bad_stack)
        assert any("retention_in_days" in f.problem for f in findings)

    def test_a_comment_naming_a_billed_resource_does_not_fail_a_build(self, tmp_path):
        """Parsed, not grepped. `main.tf` explains at length why there is no NAT
        gateway, and a grep would fail on the explanation."""
        (tmp_path / "commented.tf").write_text(
            """
            # No aws_nat_gateway here: it bills ~$32/month while idle.
            resource "aws_cloudwatch_log_group" "app" {
              name              = "/halo/app"
              retention_in_days = 14
            }
            """
        )
        assert infra.check(tmp_path) == []

    def test_the_gate_reports_each_finding_by_resource(self, monkeypatch, bad_stack):
        monkeypatch.setattr(infra, "TERRAFORM_DIR", bad_stack)
        results = gates.check_teardown()

        assert len(results) == 4
        assert not any(result.passed for result in results)


class TestCostExplorer:
    """The half that needs an account, with the account faked."""

    class Charging:
        def __init__(self, groups):
            self.groups = groups
            self.request = None

        def get_cost_and_usage(self, **kwargs):
            self.request = kwargs
            return {"ResultsByTime": [{"Groups": self.groups}]}

    def a_group(self, service: str, amount: str) -> dict:
        return {"Keys": [service], "Metrics": {"UnblendedCost": {"Amount": amount}}}

    def test_a_clean_account_reports_nothing(self):
        client = self.Charging([self.a_group("Amazon S3", "0")])
        assert infra.cost_since(client, date(2026, 9, 4), date(2026, 9, 6)) == []

    def test_what_is_still_charging_comes_back_largest_first(self):
        client = self.Charging(
            [
                self.a_group("Amazon S3", "0.0100"),
                self.a_group("EC2 - Other", "1.2000"),
                self.a_group("Amazon Cognito", "0"),
            ]
        )
        charged = infra.cost_since(client, date(2026, 9, 4), date(2026, 9, 6))

        assert charged == [("EC2 - Other", "1.2000"), ("Amazon S3", "0.0100")]

    def test_it_asks_for_daily_cost_grouped_by_service(self):
        """Grouped by service because the answer that matters is which one, and
        daily because a monthly total hides the day the destroy happened."""
        client = self.Charging([])
        infra.cost_since(client, date(2026, 9, 4), date(2026, 9, 6))

        assert client.request["Granularity"] == "DAILY"
        assert client.request["GroupBy"] == [{"Type": "DIMENSION", "Key": "SERVICE"}]

    def test_the_window_reaches_back_far_enough_to_see_yesterday(self):
        """Cost Explorer lags. A window starting today would always report zero
        and always mean nothing."""
        client = self.Charging([])
        infra.verify_teardown(client, days=2)

        start = date.fromisoformat(client.request["TimePeriod"]["Start"])
        end = date.fromisoformat(client.request["TimePeriod"]["End"])
        assert (end - start).days == 3


class TestTheCommand:
    def test_it_exits_zero_on_the_real_stack(self, capsys):
        from halo import cli

        assert cli.main(["teardown"]) == 0
        assert "survives" in capsys.readouterr().out

    def test_it_exits_non_zero_when_something_would_survive(self, monkeypatch, bad_stack, capsys):
        from halo import cli

        monkeypatch.setattr(infra, "TERRAFORM_DIR", bad_stack)
        assert cli.main(["teardown"]) == 2
        assert "aws_nat_gateway" in capsys.readouterr().out
