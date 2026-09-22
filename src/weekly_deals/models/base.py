"""Model contracts.

Two roles, kept apart:

``PromotionClassifier``  cheap triage -- does this email plausibly contain a promotion?
``OfferExtractor``  expensive structuring -- what exactly does it offer?

Neither receives credentials, tools, network access or a shell. They take text
and return data. That is the whole security boundary, and it is why the project
does not need an agent framework: there is no agent, and nothing for one to do.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..schemas import ClassificationResult, ExtractionResult, NormalizedEmail


class ModelError(RuntimeError):
    """A provider failure. Never a negative classification."""

    def __init__(self, message: str, *, code: str = "model_error", retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class BudgetExceeded(ModelError):
    def __init__(self, spent: float, limit: float) -> None:
        super().__init__(
            f"run budget exhausted: spent ${spent:.4f} of ${limit:.2f}",
            code="budget_exceeded",
            retryable=False,
        )
        self.spent = spent
        self.limit = limit


class PromotionClassifier(ABC):
    @abstractmethod
    def classify(self, email: NormalizedEmail) -> ClassificationResult:
        """Route one email. Must return a result even on failure, with error_code set."""

    @property
    def name(self) -> str:
        return type(self).__name__

    @property
    def model_id(self) -> str:
        """The model string this adapter reports in ProviderMeta.

        The cache key is built from this, so it MUST equal what ``classify``
        puts in ``meta.model``. A mismatch silently disables caching and makes
        every re-run pay again.
        """
        return type(self).__name__


class OfferExtractor(ABC):
    @abstractmethod
    def extract(self, email: NormalizedEmail) -> ExtractionResult:
        """Structure one email's offers.

        An empty ``offers`` list is only meaningful when status is SUCCESS.
        """

    @property
    def name(self) -> str:
        return type(self).__name__

    @property
    def model_id(self) -> str:
        """The model string this adapter reports in ProviderMeta.

        Must equal what ``extract`` puts in ``meta.model``: the extraction cache
        is keyed on it, and a mismatch means every re-run re-pays.
        """
        return type(self).__name__

    def capabilities(self) -> dict[str, bool]:
        """Declared capabilities. Unsupported features must degrade explicitly."""
        return {
            "structured_output": False,
            "vision": False,
            "usage_accounting": False,
        }
