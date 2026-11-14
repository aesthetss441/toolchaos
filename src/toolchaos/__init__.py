"""Record/replay and fault injection for tool-using agents."""
from .core import Fault, InjectedFault, RateLimited, RecordedError, ReplayMiss, Session, ToolTimeout
from .matrix import Outcome, resilience_matrix, to_markdown

__all__ = [
    "Fault", "InjectedFault", "Outcome", "RateLimited", "RecordedError", "ReplayMiss",
    "Session", "ToolTimeout", "resilience_matrix", "to_markdown",
]
__version__ = "0.1.0"
