"""Data source connectors for Deep Research."""

from openvarak.connectors._stubs import (
    Attachment,
    BaseConnector,
    Document,
    SyncStatus,
)
from openvarak.connectors.store import KnowledgeStore

__all__ = ["Attachment", "BaseConnector", "Document", "KnowledgeStore", "SyncStatus"]

# Auto-register built-in connectors
import openvarak.connectors.obsidian  # noqa: F401

try:
    import openvarak.connectors.gmail  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.connectors.gmail_imap  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.connectors.gdrive  # noqa: F401
except ImportError:
    pass  # httpx may not be installed

try:
    import openvarak.connectors.notion  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.connectors.granola  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.connectors.gcontacts  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.connectors.imessage  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.connectors.apple_notes  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.connectors.apple_music  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.connectors.apple_contacts  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.connectors.apple_calendar  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.connectors.slack_connector  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.connectors.outlook  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.connectors.imap  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.connectors.gcalendar  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.connectors.dropbox  # noqa: F401
except ImportError:
    pass  # httpx may not be installed

try:
    import openvarak.connectors.whatsapp  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.connectors.oura  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.connectors.apple_health  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.connectors.strava  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.connectors.spotify  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.connectors.google_tasks  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.connectors.weather  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.connectors.github_notifications  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.connectors.hackernews  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.connectors.news_rss  # noqa: F401
except ImportError:
    pass
