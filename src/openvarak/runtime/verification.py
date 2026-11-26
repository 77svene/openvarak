"""Deterministic acceptance contracts, separate from a model's self-report."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def validate_contract(contract, workspace):
    if contract is None:
        return
    if not isinstance(contract, dict):
        raise ValueError("acceptance must be an object")
    kind = contract.get("kind")
    if kind == "tool_receipts":
        names = contract.get("tools", [])
        if not isinstance(names, list) or any(
            not isinstance(n, str) or not n for n in names
        ):
            raise ValueError("acceptance.tools must be a list of tool names")
        return
    if kind not in {"file_sha256", "json_equals"}:
        raise ValueError("Unsupported acceptance kind")
    if not isinstance(contract.get("path"), str):
        raise ValueError("Acceptance path must be a string")
    root = Path(workspace).resolve()
    path = (root / contract["path"]).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("Acceptance path must stay inside the configured workspace")
    if kind == "file_sha256":
        expected = contract.get("sha256", "")
        if not isinstance(expected, str) or len(expected) != 64:
            raise ValueError("Expected sha256 must be a 64-character hex digest")
        try:
            int(expected, 16)
        except ValueError:
            raise ValueError("Expected sha256 must be hexadecimal") from None
    elif "expected" not in contract:
        raise ValueError("json_equals requires an expected value")


def verify(contract, trace, workspace):
    validate_contract(contract, workspace)
    if contract is None:
        return {"state": "unverified", "reason": "No acceptance contract supplied"}
    kind = contract["kind"]
    if kind == "tool_receipts":
        receipts = trace.get("tool_results", [])
        required = contract.get("tools", [])
        passed = bool(receipts) and all(r["success"] for r in receipts)
        passed = passed and all(
            any(r["tool"] == tool and r["success"] for r in receipts)
            for tool in required
        )
        return {"state": "verified" if passed else "failed", "kind": kind}
    if kind not in {"file_sha256", "json_equals"}:
        raise ValueError("Unsupported acceptance kind")
    root = Path(workspace).resolve()
    path = (root / contract.get("path", "")).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("Acceptance path must stay inside the configured workspace")
    try:
        data = path.read_bytes()
        if kind == "file_sha256":
            expected = contract.get("sha256", "")
            if not isinstance(expected, str) or len(expected) != 64:
                raise ValueError(
                    "Expected sha256 must contain 64 hexadecimal characters"
                )
            int(expected, 16)
            passed = hashlib.sha256(data).hexdigest() == expected.lower()
        else:
            if "expected" not in contract:
                raise ValueError("json_equals requires an expected value")
            passed = json.loads(data) == contract["expected"]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {"state": "failed", "kind": kind, "reason": type(exc).__name__}
    return {
        "state": "verified" if passed else "failed",
        "kind": kind,
        "artifact": str(path),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
