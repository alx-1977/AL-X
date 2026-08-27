"""Capability descriptions, registration, and one-call dispatch."""

from alx.capabilities.registry import CapabilityRegistry, DuplicateCapability, UnknownCapability
from alx.capabilities.broker import BrokerOutcome, BrokerState, CapabilityBroker

__all__ = ["BrokerOutcome", "BrokerState", "CapabilityBroker", "CapabilityRegistry", "DuplicateCapability", "UnknownCapability"]
