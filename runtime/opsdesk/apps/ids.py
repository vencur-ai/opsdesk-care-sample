"""Deterministic monotonic identifiers shared by state-mutating apps."""


def next_monotonic_id(prefix: str, identifiers) -> str:
    """Allocate the next numeric ID after the greatest previously issued suffix."""
    marker = f"{prefix}-"
    maximum = 0
    for identifier in identifiers:
        if not isinstance(identifier, str) or not identifier.startswith(marker):
            continue
        suffix = identifier[len(marker):]
        if suffix.isdigit():
            maximum = max(maximum, int(suffix))
    return f"{marker}{maximum + 1:06d}"
