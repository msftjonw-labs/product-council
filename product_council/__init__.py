"""Product Council: multi-model, evidence-gated deliberation on whether to productize a customer gap."""
from .config import CouncilConfig, SeatConfig, load_config
from .orchestrator import Council, tally
from .report import render_markdown

__all__ = ["Council", "CouncilConfig", "SeatConfig", "load_config", "render_markdown", "tally"]
