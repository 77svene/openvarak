"""Bounded container execution for synthesized Python tools (never host exec)."""

from __future__ import annotations

import json
import subprocess
import uuid

from openvarak.sandbox.runner import ContainerRunner

_HARNESS = """
import json, sys
payload = json.load(sys.stdin)
namespace = {"__name__": "varak_skill"}
exec(compile(payload["source"], "tool.py", "exec"), namespace)
result = namespace["execute"](**payload["params"])
print("VARAK_RESULT=" + json.dumps(result))
"""


class SkillSandbox(ContainerRunner):
    """Fresh unprivileged, network-free container for validation and each call."""

    def __init__(self, image="python:3.12-slim", timeout=30):
        super().__init__(image=image, timeout=timeout)

    def execute_source(self, source, params):
        runtime = self._check_runtime()
        name = "varak-skill-" + uuid.uuid4().hex[:12]
        args = [
            runtime,
            "run",
            "--rm",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user",
            "65534:65534",
            "--pids-limit",
            "64",
            "--memory",
            "256m",
            "--cpus",
            "1",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=16m",
            "-i",
            self._image,
            "python",
            "-I",
            "-c",
            _HARNESS,
        ]
        # Spool output to disk instead of buffering arbitrary generated output in RAM.
        import tempfile

        with tempfile.TemporaryFile(mode="w+") as output:
            try:
                proc = subprocess.run(
                    args,
                    input=json.dumps({"source": source, "params": params}),
                    text=True,
                    stdout=output,
                    stderr=output,
                    timeout=self._timeout,
                )
            except subprocess.TimeoutExpired as exc:
                self.stop(name)
                raise RuntimeError("Skill sandbox timed out") from exc
            output.seek(0, 2)
            size = output.tell()
            output.seek(max(0, size - 1024 * 1024))
            text = output.read()
        if proc.returncode:
            raise RuntimeError(
                f"Skill sandbox failed (exit {proc.returncode}): {text[-2000:]}"
            )
        lines = [line for line in text.splitlines() if line.startswith("VARAK_RESULT=")]
        if not lines:
            raise RuntimeError("Skill must return JSON-serializable output")
        return json.loads(lines[-1].removeprefix("VARAK_RESULT="))
