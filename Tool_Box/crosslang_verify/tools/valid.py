import re


def _name_tokens(name):
    """Split common HDL signal naming styles into lowercase tokens."""
    spaced = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', name)
    return [tok for tok in re.split(r'[^A-Za-z0-9]+', spaced.lower()) if tok]


def is_valid_signal(name):
    """Check whether an input name looks like a valid/valid-in signal."""
    lower = name.lower()
    if lower in {"valid", "vld", "ivalid", "invalid"}:
        return lower != "invalid"

    tokens = _name_tokens(name)
    if "invalid" in tokens:
        return False
    if "valid" in tokens or "vld" in tokens:
        return True

    return lower.endswith("valid") or lower.endswith("vld")


def get_valid_signals(inputs):
    """Return input ports that should gate output comparisons."""
    return [(name, width) for name, width in inputs if is_valid_signal(name)]
