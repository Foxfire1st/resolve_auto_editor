"""FastAPI gateway for DaVinci Resolve automation."""

from pathlib import Path
from typing import Any, Optional
import os
from dotenv import load_dotenv

# Make the path absolute, resolving all symlinks on the way and also normalizing it. Which is necessary to use resolve api within the project.
REPO_ROOT = Path(os.getcwd()).resolve()
load_dotenv(REPO_ROOT / ".env")

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from orchestrator.tool_bridge.resolve_tools import ResolveTools

app = FastAPI(title="Resolve Auto-Editor Gateway")

# Initialize ResolveTools with a default project name or from config
# For now, we'll assume a default project or let it be passed in headers/query?
# The ResolveTools client currently takes project_name in __init__.
# We might want to allow changing the project per request or have a default.
# Let's assume a default for now, or make it a dependency.

DEFAULT_PROJECT = "Test Project" # TODO: Load from settings/env

def get_resolve_tools(project_name: str = DEFAULT_PROJECT) -> ResolveTools:
    try:
        return ResolveTools(project_name=project_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to initialize ResolveTools: {e}")

class ImportFCPXMLRequest(BaseModel):
    fcpxml_path: str
    timeline_name: Optional[str] = None
    project_name: Optional[str] = None

class RenderRequest(BaseModel):
    destination: str
    preset: str = "YouTube 1080p"
    format: str = "mp4"
    codec: str = "H.264"
    project_name: Optional[str] = None

class ExportArchiveRequest(BaseModel):
    destination: str
    project_name: Optional[str] = None

class ImportRenderExportRequest(BaseModel):
    fcpxml_path: str
    render_path: str
    archive_path: str
    timeline_name: Optional[str] = None
    preset: str = "YouTube 1080p"
    format: str = "mp4"
    codec: str = "H.264"
    project_name: Optional[str] = None

@app.get("/resolve/project")
def get_project_info(project_name: str = DEFAULT_PROJECT):
    tools = get_resolve_tools(project_name)
    try:
        return tools.execute_request("get_project_info")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/resolve/timelines")
def list_timelines(project_name: str = DEFAULT_PROJECT):
    tools = get_resolve_tools(project_name)
    try:
        return tools.execute_request("list_timelines")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/resolve/import_fcpxml")
def import_fcpxml(request: ImportFCPXMLRequest):
    project = request.project_name or DEFAULT_PROJECT
    tools = get_resolve_tools(project)
    try:
        return tools.import_fcpxml(
            Path(request.fcpxml_path),
            timeline_name=request.timeline_name
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/resolve/render")
def render_timeline(request: RenderRequest):
    project = request.project_name or DEFAULT_PROJECT
    tools = get_resolve_tools(project)
    try:
        result = tools.render(
            Path(request.destination),
            preset=request.preset,
            format_=request.format,
            codec=request.codec
        )
        return {"path": str(result)}
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/resolve/export_archive")
def export_archive(request: ExportArchiveRequest):
    project = request.project_name or DEFAULT_PROJECT
    tools = get_resolve_tools(project)
    try:
        result = tools.export_timeline_archive(Path(request.destination))
        return {"path": str(result)}
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/resolve/import_render_export")
def import_render_export(request: ImportRenderExportRequest):
    project = request.project_name or DEFAULT_PROJECT
    tools = get_resolve_tools(project)
    try:
        result = tools.import_render_export(
            fcpxml_path=Path(request.fcpxml_path),
            render_path=Path(request.render_path),
            archive_path=Path(request.archive_path),
            timeline_name=request.timeline_name,
            preset=request.preset,
            format_=request.format,
            codec=request.codec
        )
        # Convert Paths to strings for JSON response
        return {k: str(v) for k, v in result.items()}
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
