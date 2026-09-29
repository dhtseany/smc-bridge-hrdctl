"""Native HRD control, as an smc-bridge plugin, for embedding, or from the command line."""
from .client import HRDClient, HRDError, ProtocolError, OutcomeUnknown
from .plugin import HrdPlugin

__all__ = ["HRDClient", "HRDError", "ProtocolError", "OutcomeUnknown", "HrdPlugin"]
