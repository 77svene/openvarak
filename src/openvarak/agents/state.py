"""Bridge persistent document backends and legacy key/value operator stores."""

from openvarak.tools.storage._stubs import MemoryBackend


def store_agent_state(backend, key, value):
    if isinstance(backend, MemoryBackend):
        if hasattr(backend, "replace_source"):
            backend.replace_source(key, [(value, None)])
        else:
            backend.store(value, source=key)
    else:
        backend.store(key, value)
