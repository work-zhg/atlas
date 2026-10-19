"""Middleware for the kernel agent.

## Overview

The LLM receives tools through two paths:

1. **SDK middleware** (this package) -- tools, system-prompt injection, and
    request interception that any SDK consumer gets automatically.
2. **Consumer-provided tools** -- plain callable functions passed via the
    `tools` parameter to `create_deep_agent()`. The CLI uses this path for
    lightweight, consumer-specific tools.

Both are merged by `create_deep_agent()` into the final tool set the LLM sees.

## Why middleware instead of plain tools?

Middleware subclasses `AgentMiddleware`, overriding its `wrap_model_call()`
hook that **intercepts every LLM request** before it is sent. This lets
middleware:

* **Filter tools dynamically** -- e.g. `FilesystemMiddleware` registers only
    the file tools on its allowlist.
* **Inject system-prompt context** -- e.g. `SkillsMiddleware` injects relevant
    instructions into the system message on every call so the LLM knows how to
    use the tools it provides.
* **Transform messages** -- e.g. `CompactionMiddleware` clears old tool
    results and replaces history with a summary when the context window fills up.
* **Maintain cross-turn state** -- middleware can read/write a typed state
    dict that persists across agent turns (e.g. `waiting_on` in `SuspensionMiddleware`).

A plain tool function in a `tools=[]` list cannot do any of this -- it is
only invoked *by* the LLM, not *before* the LLM call.

## When to use each path

Use **middleware** when the tool needs to:

* Modify the system prompt or tool list per-call
* Track state across turns
* Be available to all SDK consumers (not just the CLI)

Use a **plain tool** when:

* The function is stateless and self-contained
* No system-prompt or request modification is needed
* The tool is specific to a single consumer (e.g. CLI-only)
"""

from atlas_engine.kernel.middleware.filesystem import FilesystemMiddleware, FilesystemPermission
from atlas_engine.kernel.middleware.skills import SkillsMiddleware
from atlas_engine.kernel.middleware.subagents import (
    SubAgent,
    SubAgentMiddleware,
)

__all__ = [
    "FilesystemMiddleware",
    "FilesystemPermission",
    "SkillsMiddleware",
    "SubAgent",
    "SubAgentMiddleware",
]
