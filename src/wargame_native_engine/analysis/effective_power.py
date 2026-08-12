"""Unit effective-power calculation.

    E_i = V_i * H_i * W_i * A_i * R_i * M_i * I_i * S_i

Left-associative multiplication in ``FACTOR_ORDER``, matching the demo
``unit_power()`` semantics.  This is a **historical-replay-compatible
Native analysis candidate v1** formula — NOT an official rule.
"""

from __future__ import annotations

from wargame_native_engine.analysis._numeric import require_finite_number
from wargame_native_engine.analysis.eight_factor_contract import FACTOR_ORDER
from wargame_native_engine.analysis.models import FactorValue

EFFECTIVE_POWER_FORMULA_ID = "demo-compatible.unit-effective-power.v1"

_EXPECTED_NAMES: frozenset[str] = frozenset(FACTOR_ORDER)


def calculate_unit_effective_power(
    factor_values: tuple[FactorValue, ...],
) -> float | None:
    """Return the product of all eight factor values, or ``None``.

    Factors are validated (exactly eight, no duplicates, no unknown
    names) and multiplied left-to-right in ``FACTOR_ORDER``.  If any
    factor value is ``None`` the result is ``None``.  A result of
    ``0.0`` (e.g.  destroyed unit) is a valid effective power.
    """

    if len(factor_values) != len(FACTOR_ORDER):
        raise ValueError(
            f"expected exactly {len(FACTOR_ORDER)} factor values, "
            f"got {len(factor_values)}"
        )

    # Validate names and build ordered map.
    seen: set[str] = set()
    values: dict[str, float | None] = {}
    for fv in factor_values:
        name = fv.factor_name
        if name not in _EXPECTED_NAMES:
            raise ValueError(f"unknown factor name: {name!r}")
        if name in seen:
            raise ValueError(f"duplicate factor name: {name!r}")
        seen.add(name)
        values[name] = fv.value

    # Validate every available factor before applying UNKNOWN semantics, so
    # hand-built FactorValue records cannot hide an invalid numeric value.
    has_unknown = False
    for name in FACTOR_ORDER:
        v = values[name]
        if v is None:
            has_unknown = True
            continue
        require_finite_number(v, stage=f"effective_power factor {name}")

    if has_unknown:
        return None

    # Left-associative multiply in FACTOR_ORDER.
    product = 1.0
    for name in FACTOR_ORDER:
        v = values[name]
        assert v is not None  # established by the has_unknown check above
        product *= v
        require_finite_number(
            product,
            stage=f"effective_power product after {name}",
        )

    return product
