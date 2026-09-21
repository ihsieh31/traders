"""Re-run selected core checks hidden by the broad WebUI class-source skip."""
import inspect
import json
from pathlib import Path
import socket

import pytest

CLASSES = {
    "MalformedBrokerPayloadTests", "PositionFetchOutageTests", "CheckpointerTests",
    "HonestPresentationTests", "F12ObservationBindingTests", "F10StopCheckpointTests",
    "F15UsageTests", "F17AnalysisDateTests", "SingleEntryTests", "CallerAndExitPolicyTests",
    "ParallelCoordinatorStopTests", "R3CalendarTests", "SchedulerIntegrationTests",
}


class CoreChecks:
    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, items):
        selected = []
        for item in items:
            if getattr(getattr(item, "cls", None), "__name__", "") not in CLASSES:
                continue
            markers = [m for m in item.own_markers if m.name == "skip"
                       and m.kwargs.get("reason") == "WebUI was intentionally removed; test is obsolete"]
            if not markers:
                continue
            source = inspect.getsource(item.obj).lower()
            if any(needle in source for needle in ("webui", "run_webui_dash", "from dash", "dash.dash", "dash(")):
                continue
            item.own_markers = [m for m in item.own_markers if m not in markers]
            selected.append(item)
        items[:] = selected
        Path(__file__).with_name("rescued_core_nodeids.json").write_text(
            json.dumps([item.nodeid for item in selected], indent=2) + "\n"
        )


if __name__ == "__main__":
    def reject_network(*args, **kwargs):
        raise AssertionError("review checks must remain offline")
    socket.socket.connect = reject_network
    raise SystemExit(pytest.main(["tests", "-q", "--tb=short"], plugins=[CoreChecks()]))
