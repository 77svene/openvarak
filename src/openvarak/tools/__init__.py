"""Tools primitive — tool system with ABC interface and built-in tools."""

from __future__ import annotations

from openvarak.tools._stubs import BaseTool, ToolExecutor, ToolSpec

# Import built-in tools to trigger @ToolRegistry.register() decorators.
# Each is wrapped in try/except so the package loads even before the
# individual tool modules are created.
try:
    import openvarak.tools.calculator  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.think  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.retrieval  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.llm_tool  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.file_read  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.web_search  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.weather  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.fx_macro  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.code_interpreter  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.code_interpreter_docker  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.repl  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.storage_tools  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.mcp_adapter  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.channel_tools  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.http_request  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.docker_shell_exec  # noqa: F401
    import openvarak.tools.shell_exec  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.memory_manage  # noqa: F401
except ImportError:
    pass
try:
    import openvarak.tools.user_profile_manage  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.skill_manage  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.file_write  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.apply_patch  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.git_tool  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.db_query  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.pdf_tool  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.image_tool  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.audio_tool  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.knowledge_tools  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.text_to_speech  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.digest_collect  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.scan_chunks  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.knowledge_sql  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.apple_calendar  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.knowledge_search  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.browser  # noqa: F401
    import openvarak.tools.browser_axtree  # noqa: F401
except ImportError:
    pass

try:
    import openvarak.tools.proactive_tools  # noqa: F401
except ImportError:
    pass

__all__ = ["BaseTool", "ToolExecutor", "ToolSpec"]

import openvarak.tools.playwright_session  # noqa: F401, E402
from openvarak.core.registry import ToolRegistry  # noqa: E402
from openvarak.skills.synthesis import SynthesizeSkillTool  # noqa: E402

if not ToolRegistry.contains("synthesize_skill"):
    ToolRegistry.register_value("synthesize_skill", SynthesizeSkillTool)
