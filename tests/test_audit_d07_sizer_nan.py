"""D07 regression: PositionSizer must reject unknown numeric inputs.

Audit docs/DOCUMENTATION.md §3.3 D07: NaN
current_gross_exposure (unknown book state) is silently coerced to 0 and
the sizing approves as if the portfolio were empty; NaN atr silently uses
the default stop. Unknown exposure must fail closed — the engine cannot
prove the exposure cap with unknown book state. (risk_sizing_enabled
defaults to False, so this layer is opt-in; the audit requires the
validation to be in place before it is enabled.)
"""

import unittest

from tradingagents.risk.position_sizing import PositionSizer


class D07PositionSizerValidationTests(unittest.TestCase):
    def setUp(self):
        self.sizer = PositionSizer()

    def test_d07_nan_gross_exposure_is_rejected(self):
        decision = self.sizer.size_position(
            equity=100_000.0, price=100.0, atr=None, confidence="medium",
            requested_notional=10_000.0, current_gross_exposure=float("nan"),
        )
        self.assertFalse(
            decision.approved,
            "D07: unknown gross exposure must not be treated as an empty book",
        )

    def test_d07_nan_atr_is_rejected_without_silent_default(self):
        decision = self.sizer.size_position(
            equity=100_000.0, price=100.0, atr=float("nan"), confidence="medium",
            requested_notional=10_000.0,
        )
        self.assertFalse(decision.approved)

    def test_d07_nan_equity_and_price_stay_rejected(self):
        for kwargs in (
            dict(equity=float("nan"), price=100.0, atr=None,
                 requested_notional=10_000.0),
            dict(equity=100_000.0, price=float("nan"), atr=None,
                 requested_notional=10_000.0),
            dict(equity=100_000.0, price=100.0, atr=None,
                 requested_notional=float("nan")),
        ):
            decision = self.sizer.size_position(confidence="medium", **kwargs)
            self.assertFalse(decision.approved)

    def test_d07_valid_inputs_still_approved(self):
        decision = self.sizer.size_position(
            equity=100_000.0, price=100.0, atr=2.0, confidence="medium",
            requested_notional=10_000.0, current_gross_exposure=0.0,
        )
        self.assertTrue(decision.approved)
        self.assertGreater(decision.notional, 0.0)

    def test_d07_risk_parameters_reject_non_finite_ranges(self):
        from tradingagents.risk.position_sizing import RiskParameters

        with self.assertRaises(ValueError):
            RiskParameters(max_position_pct=float("nan"))
        with self.assertRaises(ValueError):
            RiskParameters(risk_per_trade_pct=-0.5)
        with self.assertRaises(ValueError):
            RiskParameters(atr_stop_multiplier=float("inf"))


if __name__ == "__main__":
    unittest.main()
