"""New-run candidate budget; legacy training encoders stay explicitly configurable."""
MAX_NEW_CANDIDATES = 255


def validate_new_candidate_limit(value):
    """Reject overflow; never clamp or discard authoritative legal actions."""
    if type(value) is not int or not 1 <= value <= MAX_NEW_CANDIDATES:
        raise ValueError('candidate_limit must be an exact integer in [1, 255] for new runs')
    return value
