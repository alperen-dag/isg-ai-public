"""Bounded last-known-good PPE rules, refreshed using a monotonic TTL."""
import logging
import time
from ai_service.rules import CameraPPERules


class CameraRuleCache:
    def __init__(self, loader, config, clock=time.monotonic):
        self.loader, self.config, self.clock = loader, config, clock
        self.next_refresh = float('-inf')
        self.last_success = None
        self.cached = ()
        self.failed = False

    def get(self):
        now = self.clock()
        if now >= self.next_refresh:
            self.next_refresh = now + self.config.rule_refresh_seconds
            try:
                # Reject unsupported database rules even if a legacy row is active.
                rules = []
                for rule in self.loader():
                    supported = rule.required & {'helmet', 'vest'}
                    if supported:
                        rules.append(CameraPPERules(frozenset(supported), rule.confidence_threshold))
                self.cached = tuple(rules)
                self.last_success = now
                self.failed = False
            except Exception:
                if not self.failed:
                    logging.getLogger(__name__).exception('PPE rule refresh failed; bounded cache fallback')
                self.failed = True
        if self.last_success is None or now - self.last_success >= self.config.rule_max_stale_seconds:
            return ()
        return self.cached


def changed_violation_types(before, after):
    from ai_service.rules import PPE_RULES
    def signature(rules):
        return {equipment: rule.confidence_threshold for rule in rules for equipment in rule.required}
    old, new = signature(before), signature(after)
    return {PPE_RULES[key] for key in old.keys() | new.keys() if old.get(key) != new.get(key)}
