"""Pure derived metrics (SPEC section 7). Inputs are calibrated T (deg C) and RH (%)."""

from __future__ import annotations

import math

MAGNUS_A = 17.62
MAGNUS_B = 243.12


def _svp_hpa(t: float) -> float:
    return 6.112 * math.exp(MAGNUS_A * t / (MAGNUS_B + t))


def dew_point(t: float, rh: float) -> float | None:
    if rh <= 0:
        return None
    gamma = math.log(rh / 100.0) + MAGNUS_A * t / (MAGNUS_B + t)
    return MAGNUS_B * gamma / (MAGNUS_A - gamma)


def abs_humidity(t: float, rh: float) -> float:
    """g/m3."""
    return 216.7 * (rh / 100.0 * _svp_hpa(t)) / (273.15 + t)


def emc(t: float, rh: float) -> float | None:
    """Wood equilibrium moisture content (%), Hailwood-Horrobin per the USDA Wood Handbook."""
    if not 0 <= rh <= 100:
        return None
    tf = t * 9 / 5 + 32
    h = rh / 100.0
    w = 330 + 0.452 * tf + 0.00415 * tf**2
    k = 0.791 + 4.63e-4 * tf - 8.44e-7 * tf**2
    k1 = 6.34 + 7.75e-4 * tf - 9.35e-5 * tf**2
    k2 = 1.09 + 2.84e-2 * tf - 9.04e-5 * tf**2
    kh = k * h
    return (
        1800
        / w
        * (kh / (1 - kh) + (k1 * kh + 2 * k1 * k2 * kh**2) / (1 + k1 * kh + k1 * k2 * kh**2))
    )


def round_or_none(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)
