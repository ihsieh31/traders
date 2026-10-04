"""Market-structure flags fed to the market analyst's technical brief.

BOS must fire in both directions (a breakdown below the last swing low is a
break of structure, not only a breakout above the last swing high), and
CHOCH means a break AGAINST the prevailing swing structure, not merely an
expanding or contracting swing range.
"""

import numpy as np
import pandas as pd

from tradingagents.dataflows.technical_brief import detect_market_structure


def _frame(path):
    """Bars around a piecewise-linear mid path given as (target, bars) legs."""
    mids = [float(path[0])]
    for target, bars in path[1:]:
        mids.extend(np.linspace(mids[-1], target, bars + 1)[1:])
    mids = np.asarray(mids)
    return pd.DataFrame({
        "open": mids, "high": mids + 0.5, "low": mids - 0.5, "close": mids,
        "volume": np.full(mids.size, 1_000.0),
    })


# Uptrend swings: highs 20 -> 25 (HH), lows 15 -> 19 (HL).
UPTREND = [10, (20, 8), (15, 6), (25, 8), (19, 6)]
# Downtrend swings: highs 30 -> 25 (LH), lows 22 -> 17 (LL).
DOWNTREND = [35, (30, 6), (22, 8), (25, 4), (17, 8)]


def test_uptrend_continuation_is_bos_not_choch():
    structure = detect_market_structure(_frame(UPTREND + [(27, 8)]))
    assert structure.last_swing_high == 25.5
    assert structure.bos and not structure.choch


def test_breakdown_below_last_swing_low_is_a_bearish_bos():
    structure = detect_market_structure(_frame(DOWNTREND + [(21, 4), (15, 6)]))
    assert structure.last_swing_low == 16.5
    assert structure.bos and not structure.choch


def test_losing_the_last_higher_low_is_a_change_of_character():
    structure = detect_market_structure(_frame(UPTREND + [(23, 4), (17, 6)]))
    assert structure.last_swing_low == 18.5
    assert structure.bos and structure.choch


def test_reclaiming_the_last_lower_high_is_a_change_of_character():
    structure = detect_market_structure(_frame(DOWNTREND + [(21, 4), (18, 4), (27, 8)]))
    assert structure.last_swing_high == 21.5
    assert structure.bos and structure.choch


def test_expanding_range_without_a_break_is_neither():
    # Highs 20 -> 26 (HH) and lows 15 -> 12 (LL), close back inside the range.
    structure = detect_market_structure(_frame([10, (20, 8), (15, 6), (26, 8), (12, 8), (18, 5)]))
    assert not structure.bos and not structure.choch
