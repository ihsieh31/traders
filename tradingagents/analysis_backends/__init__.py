"""Analysis backend selection and implementations.

An analysis backend owns only the upstream research topology.  The report
context and every downstream decision node remain shared by the A/B arms.
"""

from .resolver import VALID_ANALYSIS_BACKENDS, resolve_analysis_backend

__all__ = ["VALID_ANALYSIS_BACKENDS", "resolve_analysis_backend"]
