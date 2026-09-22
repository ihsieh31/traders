import pytest


class MixedLegacyTests:
    def test_removed_webui_behavior(self):
        pytest.fail("the explicitly removed WebUI test should remain skipped")

    def test_core_behavior_is_not_skipped_with_removed_webui_test(self):
        assert True
