"""
Provider abstraction base interface.

All cloud providers (AWS, Azure, GCP) should implement this interface
when real provider integrations are added in later phases.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict


class BaseCloudProvider(ABC):
    """Abstract interface for cloud provider operations."""

    @abstractmethod
    def validate_credentials(self, **kwargs) -> bool:
        """Validate the provided credentials for this cloud provider."""
        ...

    @abstractmethod
    def get_account_info(self, **kwargs) -> Dict[str, Any]:
        """Retrieve account/project/subscription metadata."""
        ...
