"""atlas-bus — ATLAS agent bus: discovery, isolation, and pipeline routing."""

from atlas_bus.bus import Bus, PipelineResult
from atlas_bus.runner import StepResult

__version__ = "1.0.0"
__all__ = ["Bus", "PipelineResult", "StepResult"]
