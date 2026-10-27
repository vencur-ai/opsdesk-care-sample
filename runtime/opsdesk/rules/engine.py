"""Select and run the rules that apply to a world."""

from __future__ import annotations

from opsdesk.core.models import Jurisdiction
from opsdesk.core.state import WorldState
from opsdesk.rules.base import Rule, Violation, check_rules
from opsdesk.rules.house_rules import build_house_rule_pack
from opsdesk.rules.pack_de import german_rule_pack
from opsdesk.rules.pack_uk import uk_rule_pack


def rules_for_world(state: WorldState) -> tuple[Rule, ...]:
    """Return the jurisdiction and home rules applicable to a state."""
    legal_pack = uk_rule_pack() if state.jurisdiction == Jurisdiction.UK else german_rule_pack()
    house_pack = build_house_rule_pack(state.house_rule_policy)
    return (*legal_pack.rules, *house_pack.rules)


def check_world_rules(state: WorldState) -> list[Violation]:
    """Check all active rules in stable order."""
    return check_rules(state, rules_for_world(state))