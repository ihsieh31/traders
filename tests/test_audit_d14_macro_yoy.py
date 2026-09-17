"""D14 regression: YoY must compare the same calendar month last year.

Audit docs/AUDIT_SECOND_OPINION_2026-09-17.md §3.3 D14: valid_obs[11] is
merely the 11th previous *observation* — with any missing month it is not
the year-ago value. The comparison must match last year's calendar month
of the latest observation's date, and return no YoY line when that month
is unavailable.
"""

import unittest


class D14MacroYoYTests(unittest.TestCase):
    def _report_for(self, observations):
        from tradingagents.dataflows.macro_utils import _format_indicator_section

        return _format_indicator_section("CPI", {
            "series": "CPIAUCSL", "description": "Consumer Price Index",
            "unit": "Index", "yoy": True,
        }, observations)

    def test_d14_yoy_uses_same_calendar_month(self):
        observations = [
            {"date": "2026-08-01", "value": "110.0"},  # latest: Aug 2026
            *[{"date": f"2026-{m:02d}-01", "value": "100.0"}
              for m in range(7, 0, -1)],
            {"date": "2025-12-01", "value": "100.0"},
            {"date": "2025-11-01", "value": "99.0"},
            {"date": "2025-10-01", "value": "98.0"},
            {"date": "2025-09-01", "value": "97.0"},
            {"date": "2025-08-01", "value": "104.0"},  # true year-ago (Aug 2025)
            {"date": "2025-07-01", "value": "96.0"},
            {"date": "2025-06-01", "value": "95.0"},
            {"date": "2025-05-01", "value": "94.0"},
            {"date": "2025-04-01", "value": "93.0"},
            {"date": "2025-03-01", "value": "93.5"},
            {"date": "2025-02-01", "value": "93.2"},
        ]
        section = self._report_for(observations)
        # 110 vs 104 => +5.77%; the old 11-observation-ago value would be
        # 93.2 (Feb 2025) -> +18%, visibly wrong.
        self.assertIn("+5.77%", section)

    def test_d14_missing_year_ago_month_reports_no_yoy(self):
        observations = [
            {"date": "2026-08-01", "value": "110.0"},
            # August 2025 is absent from the window.
            *[{"date": f"2025-{m:02d}-01", "value": "95.0"} for m in range(7, 0, -1)],
            {"date": "2024-12-01", "value": "94.0"},
        ]
        section = self._report_for(observations)
        self.assertNotIn("Year-over-Year", section)


if __name__ == "__main__":
    unittest.main()
