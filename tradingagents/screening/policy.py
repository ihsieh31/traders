"""Selection policy shared by scanning, cache validation and unattended setup."""

import math
from dataclasses import dataclass


def screening_method(config):
    method = config.get("screening_method", "auto")
    if method not in ("auto", "exclusion", "legacy"):
        raise ValueError("screening_method must be auto, exclusion or legacy")
    shorts = bool(config.get("allow_shorts", False))
    if method == "exclusion" and shorts:
        raise ValueError("the exclusion formula is long-only; use auto or legacy for shorts")
    return ("legacy" if shorts else "exclusion") if method == "auto" else method


@dataclass(frozen=True)
class ExclusionThresholds:
    max_vol20: float = 0.28
    min_r60: float = -0.25

    @classmethod
    def from_config(cls, config):
        values = []
        for key, default in (("screening_max_vol20", cls.max_vol20),
                             ("screening_min_r60", cls.min_r60)):
            raw = config.get(key, default)
            if isinstance(raw, bool):
                raise ValueError(f"{key} must be finite")
            value = float(raw)
            if not math.isfinite(value):
                raise ValueError(f"{key} must be finite")
            values.append(value)
        if values[0] <= 0 or not -1 <= values[1] <= 0:
            raise ValueError("screening_max_vol20 must be positive; screening_min_r60 must be in [-1, 0]")
        return cls(*values)


def validate_screening_policy(config):
    from .metrics import EligibilityThresholds

    method = screening_method(config)
    EligibilityThresholds.from_config(config)
    for key, default in (("screening_select_n", 20), ("screening_top_k", 40),
                         ("screening_max_per_sector", 5), ("screening_analysis_limit", 20)):
        raw = config.get(key, default)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
            raise ValueError(f"{key} must be a positive integer")
    if config.get("screening_select_n", 20) > 20 or config.get("screening_analysis_limit", 20) > 20:
        raise ValueError("screening allows at most 20 selected/analyzed stocks")
    if method == "exclusion":
        ExclusionThresholds.from_config(config)
    elif config.get("screening_top_k", 40) < config.get("screening_select_n", 20):
        raise ValueError("screening_top_k must be >= screening_select_n for legacy screening")
    return method
