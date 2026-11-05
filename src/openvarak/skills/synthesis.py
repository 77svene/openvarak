"""Validate, persist, and hot-load Agent Skills manifests and Python tools."""

from __future__ import annotations

import ast
import json
import re
import shutil
import tempfile
import threading
from pathlib import Path

from openvarak.core.paths import get_config_dir
from openvarak.core.registry import ToolRegistry
from openvarak.core.types import ToolResult
from openvarak.sandbox.skills import SkillSandbox
from openvarak.tools._stubs import BaseTool, ToolSpec


class SynthesizedSkillTool(BaseTool):
    def __init__(self, name, description, parameters, source, sandbox):
        self._spec = ToolSpec(
            name=name,
            description=description,
            parameters=parameters,
            category="synthesized",
            timeout_seconds=40,
            required_capabilities=["code:execute"],
        )
        self.source, self.sandbox = source, sandbox
        self.tool_id = name

    @property
    def spec(self):
        return self._spec

    def execute(self, **params):
        try:
            output = self.sandbox.execute_source(self.source, params)
            return ToolResult(
                tool_name=self.tool_id, content=json.dumps(output), success=True
            )
        except Exception as exc:
            return ToolResult(tool_name=self.tool_id, content=str(exc), success=False)


class SkillSynthesizer:
    def __init__(self, skills_dir=None, sandbox=None):
        self.root = Path(skills_dir or get_config_dir() / "skills" / "synthesized")
        self.root.mkdir(parents=True, exist_ok=True)
        self.sandbox = sandbox or SkillSandbox()
        self._lock = threading.RLock()
        self._owned = set()

    def synthesize(
        self,
        name,
        description,
        source,
        parameters=None,
        test_params=None,
        expected_result=None,
        replace=False,
    ):
        if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", name) or len(name) > 64:
            raise ValueError(
                "Skill name must be 1–64 lowercase letters, digits, and hyphens"
            )
        if (
            not isinstance(description, str)
            or not description.strip()
            or len(description) > 1024
        ):
            raise ValueError("Description must contain 1–1024 characters")
        tree = ast.parse(source)
        if not any(
            isinstance(n, ast.FunctionDef) and n.name == "execute" for n in tree.body
        ):
            raise ValueError("tool.py must define execute(**params) returning JSON")
        if len(source.encode()) > 128 * 1024:
            raise ValueError("Skill source exceeds 128 KiB")
        parameters = parameters or {"type": "object", "properties": {}}
        if parameters.get("type") != "object":
            raise ValueError("Tool parameters must be an object schema")
        with self._lock:
            if ToolRegistry.contains(name):
                registered = ToolRegistry.get(name)
                same_root = (
                    getattr(registered, "varak_skill_root", None) == self.root.resolve()
                )
                if not (replace and (name in self._owned or same_root)):
                    raise ValueError("Cannot overwrite another tool")
            # Validation must succeed before filesystem or registry mutation.
            actual = self.sandbox.execute_source(source, test_params or {})
            if expected_result is not None and actual != expected_result:
                raise ValueError("Sandbox validation did not match expected_result")
            manifest = {
                "name": name,
                "description": description,
                "parameters": parameters,
                "test_params": test_params or {},
                "expected_result": expected_result,
            }
            tool = SynthesizedSkillTool(
                name, description, parameters, source, self.sandbox
            )
            destination = self.root / name
            staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=self.root))
            backup = self.root / f".backup-{name}"
            try:
                (staging / "scripts").mkdir()
                # JSON string scalars are also valid YAML scalars.
                (staging / "SKILL.md").write_text(
                    f"---\nname: {name}\n"
                    f"description: {json.dumps(description)}\n---\n\n"
                    f"# {name}\n\n{description}\n\n"
                    "Run scripts/tool.py through the OpenVarak "
                    "skill sandbox.\n",
                    encoding="utf-8",
                )
                (staging / "scripts" / "tool.py").write_text(source, encoding="utf-8")
                (staging / "tool.json").write_text(
                    json.dumps(manifest), encoding="utf-8"
                )
                if backup.exists():
                    shutil.rmtree(backup)
                if destination.exists():
                    destination.rename(backup)
                try:
                    staging.rename(destination)
                except Exception:
                    if backup.exists():
                        backup.rename(destination)
                    raise
                self._register(name, tool)
                if backup.exists():
                    shutil.rmtree(backup)
                return tool
            finally:
                if staging.exists():
                    shutil.rmtree(staging)

    def _register(self, name, tool):
        # Registry stores constructors, matching built-in tools. Existing executors
        # receive the validated instance explicitly through install_tool().
        def factory():
            return tool

        factory.varak_skill_root = self.root.resolve()
        ToolRegistry.replace_value(name, factory)
        self._owned.add(name)

    def load_saved(self):
        loaded = []
        for path in sorted(self.root.glob("*/tool.json")):
            manifest = json.loads(path.read_text(encoding="utf-8"))
            loaded.append(
                self.synthesize(
                    source=(path.parent / "scripts" / "tool.py").read_text(
                        encoding="utf-8"
                    ),
                    replace=True,
                    **manifest,
                )
            )
        return loaded


class SynthesizeSkillTool(BaseTool):
    tool_id = "synthesize_skill"

    def __init__(self, synthesizer=None):
        self.synthesizer = synthesizer or SkillSynthesizer()
        self._executor = None

    def bind_executor(self, executor):
        self._executor = executor

    @property
    def spec(self):
        return ToolSpec(
            name=self.tool_id,
            description=(
                "Author and test a persistent Agent Skill. "
                "Python source must define execute(**params) "
                "returning JSON. Validation and execution use an isolated container."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "source": {"type": "string"},
                    "parameters": {"type": "object"},
                    "test_params": {"type": "object"},
                    "expected_result": {},
                },
                "required": ["name", "description", "source", "test_params"],
            },
            timeout_seconds=90,
            required_capabilities=["code:execute", "file:write"],
        )

    def execute(self, **params):
        try:
            tool = self.synthesizer.synthesize(**params)
            if self._executor:
                self._executor.install_tool(tool)
            return ToolResult(
                tool_name=self.tool_id, content=f"Loaded {tool.spec.name}", success=True
            )
        except Exception as exc:
            return ToolResult(tool_name=self.tool_id, content=str(exc), success=False)
