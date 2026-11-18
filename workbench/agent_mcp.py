#!/usr/bin/env python3
"""Stdio MCP tools for a user-owned agent to operate the existing workbench."""
import json
import os
from pathlib import Path
from re import fullmatch
import subprocess
import sys
from typing import Literal
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

PORT = int(os.environ.get("COLAB_WORKBENCH_PORT", "8765"))
ORIGIN = f"http://127.0.0.1:{PORT}"
SERVER = Path(__file__).with_name("app.py")
mcp = FastMCP("colab-workbench", instructions=(
    "Use these tools for storyboards and video production in the local workbench. "
    "Stop for human review before approving clips. Only submit a paid render when the user requests it."
))
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)


def identifier(value):
    if not fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", value):
        raise ValueError("Invalid project or job identifier.")
    return value


def request(path, value=None):
    data = None if value is None else json.dumps(value, ensure_ascii=False).encode()
    req = Request(ORIGIN+path, data=data, headers={
        "Origin": ORIGIN, "X-Workbench": "1", "Content-Type": "application/json",
    })
    try:
        with urlopen(req, timeout=40) as res:
            return json.load(res)
    except HTTPError as error:
        try:
            message = json.load(error).get("error")
        except (ValueError, AttributeError):
            message = None
        raise RuntimeError(message or f"Workbench returned HTTP {error.code}") from None


@mcp.tool(annotations=READ)
def list_projects() -> list[dict]:
    """List editable project IDs and titles."""
    return request("/api/projects")


@mcp.tool(annotations=READ)
def read_project(project_id: str) -> dict:
    """Read the current revision, manifest, takes, transcript, and exports for a project."""
    return dict(request("/api/project/" + identifier(project_id)), local_api=ORIGIN)


@mcp.tool()
def create_draft(title: str) -> dict:
    """Create an editable six-second draft with starter character references. This allocates no GPU."""
    return request("/api/new", {"title": title})


@mcp.tool()
def save_storyboard(project_id: str, revision: str, manifest: dict) -> dict:
    """Save an editable storyboard. Pass the revision from read_project and preserve referenced assets.
    Shots use 3–12 integer seconds, unique IDs, a location, known cast, action, and optional ordered dialogue.
    Set target_seconds to the exact sum. Saving does not allocate a GPU.
    """
    return request("/api/project/" + identifier(project_id), {"revision": revision, "manifest": manifest})


@mcp.tool()
def submit_task(project_id: str, action: Literal["generate", "qa", "approve", "cut"],
                shots: list[str] | None = None, gpu_authorized: bool = False,
                reviewed: bool = False) -> dict:
    """Queue a task. Set gpu_authorized=true only after the user requests a Colab render.
    Set reviewed=true only for takes the user has confirmed. A cut requires approved takes.
    """
    if action == "generate" and not gpu_authorized:
        raise ValueError("Colab generation requires the user's authorization to spend compute units.")
    if action == "approve" and not reviewed:
        raise ValueError("Confirm human review before approving a take.")
    if action != "cut" and not shots:
        raise ValueError("Provide the explicit shot IDs for this task.")
    return request("/api/run", {"project": identifier(project_id), "action": action, "shots": shots or []})


@mcp.tool(annotations=READ)
def read_job(job_id: str) -> dict:
    """Read the state and redacted log for a queued or finished workbench task."""
    return request("/api/job/" + identifier(job_id))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True))
def colab_usage() -> dict:
    """Query the connected Google account's current compute balance and active GPU assignments."""
    return request("/api/usage")


if __name__ == "__main__":
    subprocess.run([sys.executable, str(SERVER), "ensure", "--port", str(PORT)],
                   check=True, timeout=35, capture_output=True)
    mcp.run(transport="stdio")
