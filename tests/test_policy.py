from __future__ import annotations

import pytest

from inet_helpdesk_mcp.errors import ConfigurationError, HelpdeskError
from inet_helpdesk_mcp.policy import (
    ActionPolicy,
    parse_action_ids,
    validate_automail,
)


def test_parse_action_ids_ignores_blanks() -> None:
    assert parse_action_ids(" -9 , -12 ,, ") == ("-9", "-12")
    assert parse_action_ids(None) == ()


def test_an_empty_policy_permits_everything() -> None:
    policy = ActionPolicy()

    assert policy.permits("-12")
    assert not policy.restricts_actions


def test_allowlist_blocks_everything_else() -> None:
    policy = ActionPolicy(allowed=("-9",))

    assert policy.permits("-9")
    assert not policy.permits("-12")


def test_denylist_wins_over_the_allowlist() -> None:
    policy = ActionPolicy(allowed=("-9", "-12"), denied=("-12",))

    assert not policy.permits("-12")


def test_check_names_the_allowed_actions() -> None:
    policy = ActionPolicy(allowed=("-9",))

    with pytest.raises(HelpdeskError) as excinfo:
        policy.check("-12")

    assert "-12" in str(excinfo.value)
    assert "-9" in str(excinfo.value)


def test_filter_actions_counts_what_it_hides() -> None:
    policy = ActionPolicy(denied=("-2",))

    kept, hidden = policy.filter_actions({"-9": "E-Mail", "-2": "Reaktivieren"})

    assert kept == {"-9": "E-Mail"}
    assert hidden == 1


def test_automail_default_does_not_overwrite_the_caller() -> None:
    policy = ActionPolicy(default_automail="NEVER")

    assert policy.apply_automail(None) == {"ticketextension.automail": "NEVER"}
    assert policy.apply_automail({"ticketextension.automail": "ALWAYS"}) == {
        "ticketextension.automail": "ALWAYS"
    }


def test_without_a_default_the_arguments_stay_as_they_are() -> None:
    policy = ActionPolicy()

    assert policy.apply_automail(None) is None
    assert policy.apply_automail({"a": "b"}) == {"a": "b"}


def test_validate_automail_normalises_and_rejects() -> None:
    assert validate_automail("never") == "NEVER"
    assert validate_automail(None) is None

    with pytest.raises(ConfigurationError, match="NO_MAILS_TO_ENDUSER"):
        validate_automail("QUATSCH")
