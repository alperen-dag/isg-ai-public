"""Explicit label aliases only. Never turn a positive class into an absence."""
ALIASES = {
    'hardhat': 'helmet', 'helmet': 'helmet',
    'no_hardhat': 'no_helmet', 'no_helmet': 'no_helmet',
    'safety_vest': 'vest', 'vest': 'vest',
    'no_safety_vest': 'no_vest', 'no_vest': 'no_vest',
    'ear_protection': 'ear_protection', 'no_ear_protection': 'no_ear_protection',
    'safety_shoes': 'safety_shoes', 'no_safety_shoes': 'no_safety_shoes',
}


def canonical_class(name):
    return ALIASES.get('_'.join(name.strip().lower().replace('-', ' ').replace('_', ' ').split()))
