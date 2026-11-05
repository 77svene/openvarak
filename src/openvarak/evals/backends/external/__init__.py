"""External-framework subprocess backends (Hermes Agent, OpenClaw)."""

from openvarak.evals.backends.external.hermes_agent import HermesBackend
from openvarak.evals.backends.external.openclaw import OpenClawBackend

__all__ = ["HermesBackend", "OpenClawBackend"]
