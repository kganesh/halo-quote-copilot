"""Claims in, `Principal` out — and every way that is refused.

Admission is the narrowest place in the system: one function, and everything
downstream trusts what comes out of it. So most of these tests are about what it
will not do. A missing claim that produced an empty scope, or a default role,
would fail somewhere far away and look like a different bug entirely.
"""

import pytest

from halo.platform.admission import (
    ACCOUNTS_CLAIM,
    GROUPS_CLAIM,
    TENANT_CLAIM,
    AdmissionError,
    principal_from_claims,
    principal_from_event,
)
from halo.platform.identity import Role

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_practice"


def claims(**overrides) -> dict:
    return {
        "sub": "usr-mwes01",
        "iss": ISSUER,
        "aud": "5f3c9practiceclientid",
        "token_use": "id",
        GROUPS_CLAIM: "halo-seller",
        TENANT_CLAIM: "tnt-mwest1",
        ACCOUNTS_CLAIM: "acct-mwes02,acct-mwes03",
        **overrides,
    }


class TestAdmitting:
    def test_claims_become_a_frozen_principal(self):
        principal = principal_from_claims(claims())

        assert principal.user_id == "usr-mwes01"
        assert principal.tenant_id == "tnt-mwest1"
        assert principal.role is Role.SELLER
        assert principal.account_ids == ("acct-mwes02", "acct-mwes03")

    def test_the_account_list_survives_spacing_and_duplication(self):
        principal = principal_from_claims(
            claims(**{ACCOUNTS_CLAIM: " acct-mwes02 , acct-mwes03,acct-mwes02 "})
        )
        assert principal.account_ids == ("acct-mwes02", "acct-mwes03")

    def test_a_real_json_list_is_accepted_too(self):
        """A Lambda authorizer can return a list. Cognito's own custom
        attributes cannot, so both shapes arrive in practice."""
        principal = principal_from_claims(claims(**{ACCOUNTS_CLAIM: ["acct-mwes02"]}))
        assert principal.account_ids == ("acct-mwes02",)

    def test_the_most_privileged_group_wins(self):
        principal = principal_from_claims(
            claims(**{GROUPS_CLAIM: "halo-seller,halo-sales-manager"})
        )
        assert principal.role is Role.SALES_MANAGER

    def test_issuer_and_audience_are_checked_when_configured(self):
        principal = principal_from_claims(claims(), issuer=ISSUER, audience="5f3c9practiceclientid")
        assert principal.user_id == "usr-mwes01"


class TestRefusing:
    def test_a_token_from_another_pool_is_refused(self):
        """The signature is valid and the person is a stranger. This is what a
        route wired to the wrong user pool looks like from in here."""
        with pytest.raises(AdmissionError, match="issuer"):
            principal_from_claims(
                claims(iss="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_other"),
                issuer=ISSUER,
            )

    def test_the_issuer_check_is_opt_in(self):
        """Nothing here knows which pool is ours until it is configured. The
        deployed path passes `issuer`; a local run has no pool to compare to,
        and inventing a default would make the check meaningless in both."""
        assert principal_from_claims(claims(iss="https://example.invalid/pool")).user_id

    def test_a_token_for_another_application_is_refused(self):
        with pytest.raises(AdmissionError, match="audience"):
            principal_from_claims(claims(), audience="a-different-client")

    def test_an_access_token_is_refused_by_name(self):
        """It is signed by the same key and carries no custom attributes. Saying
        so beats an error about a missing tenant claim."""
        with pytest.raises(AdmissionError, match="ID token"):
            principal_from_claims(claims(token_use="access"))

    def test_an_expired_token_is_refused(self):
        with pytest.raises(AdmissionError, match="expired"):
            principal_from_claims(claims(exp=1_000), now=2_000)

    def test_a_token_still_valid_is_admitted(self):
        assert principal_from_claims(claims(exp=3_000), now=2_000).user_id == "usr-mwes01"

    @pytest.mark.parametrize("missing", ["sub", TENANT_CLAIM])
    def test_a_missing_claim_is_an_error_not_a_default(self, missing):
        with pytest.raises(AdmissionError, match="missing"):
            principal_from_claims(claims(**{missing: ""}))

    def test_an_empty_account_list_is_refused_rather_than_admitted_with_no_scope(self):
        """A principal with no accounts would be denied everywhere and look like
        a bug in the tools rather than a bug in the directory."""
        with pytest.raises(AdmissionError, match="no scope"):
            principal_from_claims(claims(**{ACCOUNTS_CLAIM: ""}))

    @pytest.mark.parametrize("groups", ["", "some-other-group", "halo-sellers"])
    def test_an_unmapped_group_grants_nothing(self, groups):
        """Including the plausible near-miss. A typo in a group name must not
        quietly produce a role."""
        with pytest.raises(AdmissionError, match="role"):
            principal_from_claims(claims(**{GROUPS_CLAIM: groups}))


class TestFromAnEvent:
    def event(self, **overrides) -> dict:
        return {"requestContext": {"authorizer": {"jwt": {"claims": claims(**overrides)}}}}

    def test_an_authorized_event_admits(self):
        assert principal_from_event(self.event()).tenant_id == "tnt-mwest1"

    def test_an_event_with_no_authorizer_is_refused(self):
        """Not an anonymous caller. An unprotected route, which is worse."""
        with pytest.raises(AdmissionError, match="not protected"):
            principal_from_event({"requestContext": {}})
