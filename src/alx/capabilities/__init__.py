"""Capability descriptions, registration, and one-call dispatch."""

from alx.capabilities.registry import CapabilityRegistry, DuplicateCapability, UnknownCapability
from alx.capabilities.broker import CapabilityBroker

__all__ = ["CapabilityBroker", "CapabilityRegistry", "DuplicateCapability", "UnknownCapability"]
