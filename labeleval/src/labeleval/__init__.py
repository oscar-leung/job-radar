"""labeleval — grade an LLM (or any scorer) against human labels."""
from .core import Case, Report, compare, evaluate, faithfulness, load_cases
from .metrics import (Confusion, at_threshold, best_threshold, bootstrap_ci, cohen_kappa,
                      confusion, sweep)

__all__ = ["Case", "Report", "compare", "evaluate", "faithfulness", "load_cases", "Confusion",
           "at_threshold", "best_threshold", "bootstrap_ci", "cohen_kappa", "confusion", "sweep"]
__version__ = "0.1.0"
