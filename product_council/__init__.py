"""Product Council: multi-model, evidence-gated deliberation on whether to productize a customer gap."""
from .config import CouncilConfig, SeatConfig, load_config
from .orchestrator import Council, tally
from .report import render_markdown
from .schemas import ProductBrief

__all__ = ["Council", "CouncilConfig", "SeatConfig", "ProductBrief", "load_config", "render_markdown", "tally"]
