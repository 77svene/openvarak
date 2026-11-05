"""External anonymous usage analytics.

Sends anonymized events to PostHog so the OpenVarak team can measure
setup success, retention, feature usage, and churn — without ever
collecting chat content, prompts, file paths, emails, IPs, or hardware
identifiers.

Distinct from :mod:`openvarak.telemetry`, which stores local FLOPs and
energy metrics in a SQLite DB and never leaves the machine.

Disable: set ``[analytics] enabled = false`` in ``~/.openvarak/config.toml``.
"""

from openvarak.analytics.aggregator import SessionAggregator
from openvarak.analytics.bridge import EventBridge
from openvarak.analytics.client import AnalyticsClient
from openvarak.analytics.identity import (
    get_or_create_anon_id,
    is_analytics_enabled,
    reset_anon_id,
)
from openvarak.analytics.redaction import hash_id, redact

__all__ = [
    "AnalyticsClient",
    "EventBridge",
    "SessionAggregator",
    "get_or_create_anon_id",
    "is_analytics_enabled",
    "reset_anon_id",
    "redact",
    "hash_id",
]
