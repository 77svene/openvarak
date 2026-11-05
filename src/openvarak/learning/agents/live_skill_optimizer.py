"""Online failed-trace optimizer, without offline benchmark dependencies."""

from __future__ import annotations

import json

from openvarak.core.types import Message, Role
from openvarak.skills.synthesis import SynthesizedSkillTool


class LiveSkillOptimizer:
    """Repair synthesized code or refine a built-in's prompt/schema description.

    Code patches retain the original regression check and must also pass the
    failed call in the sandbox. Built-ins are never overwritten with generated
    host code. Failed optimization leaves the last working tool intact.
    """

    def __init__(self, engine, model, synthesizer, memory=None):
        self.engine, self.model = engine, model
        self.synthesizer, self.memory = synthesizer, memory

    def __call__(self, tool, call, result):
        trace = {
            "tool": call.name,
            "arguments": call.arguments,
            "failure": result.content,
        }
        if self.memory:
            self.memory.checkpoint(call.name, trace, kind="failed_trace")
        if isinstance(tool, SynthesizedSkillTool):
            manifest = json.loads(
                (self.synthesizer.root / call.name / "tool.json").read_text()
            )
            prompt = (
                "Repair this Python tool using the failed trace. "
                "Return ONLY a JSON object "
                "with source (complete Python execute function). "
                "Preserve its interface.\n"
                + json.dumps({"trace": trace, "source": tool.source})
            )
        else:
            prompt = (
                "Improve this tool description to help the agent recover "
                "from the failed call. "
                "Return ONLY JSON with description. Preserve the actual interface.\n"
                + json.dumps(
                    {
                        "trace": trace,
                        "description": tool.spec.description,
                        "parameters": tool.spec.parameters,
                    }
                )
            )
        response = self.engine.generate(
            [Message(role=Role.USER, content=prompt)],
            model=self.model,
            retry_count=2,
            max_tokens=4096,
            response_format={"type": "json_object"},
        )
        proposal = json.loads(response["content"])
        if isinstance(tool, SynthesizedSkillTool):
            source = proposal["source"]
            # Regression validation occurs before replacing the saved artifact.
            self.synthesizer.sandbox.execute_source(
                source, json.loads(call.arguments or "{}")
            )
            return self.synthesizer.synthesize(source=source, replace=True, **manifest)
        from dataclasses import replace

        from openvarak.agents.tool_resolver import _SpecOverrideTool

        description = proposal["description"]
        if not isinstance(description, str) or not description.strip():
            raise ValueError("Optimizer returned an empty description")
        spec = replace(tool.spec, description=description)
        return _SpecOverrideTool(
            tool,
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            },
        )

    def recover_task(self, prompt, failed_trace):
        """Derive one bounded recovery prompt after repeated task failure."""
        if self.memory:
            self.memory.checkpoint("desktop", failed_trace, kind="failed_task")
        result = self.engine.generate(
            [
                Message(
                    role=Role.USER,
                    content=(
                        "Propose a concise recovery plan for this failed task. "
                        "Preserve the original objective; use the failure evidence.\n"
                        + json.dumps({"prompt": prompt, "trace": failed_trace})
                    ),
                )
            ],
            model=self.model,
            retry_count=2,
            max_tokens=1024,
        )
        return (
            prompt + "\n\nRecovery plan after repeated failures:\n" + result["content"]
        )
