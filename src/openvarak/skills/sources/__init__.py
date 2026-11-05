"""Skill source resolvers — Hermes, OpenClaw, generic GitHub."""

from openvarak.skills.sources.base import ResolvedSkill, SourceResolver
from openvarak.skills.sources.github import GitHubResolver
from openvarak.skills.sources.hermes import HERMES_REPO_URL, HermesResolver
from openvarak.skills.sources.openclaw import OPENCLAW_REPO_URL, OpenClawResolver

__all__ = [
    "GitHubResolver",
    "HERMES_REPO_URL",
    "HermesResolver",
    "OPENCLAW_REPO_URL",
    "OpenClawResolver",
    "ResolvedSkill",
    "SourceResolver",
]
