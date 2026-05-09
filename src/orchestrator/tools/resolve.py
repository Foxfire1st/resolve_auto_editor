from pathlib import Path
from typing import Any, Dict
from src.orchestrator import models
from src.orchestrator.tool_bridge.otio_tools import build_and_export_semantic_edit
from src.orchestrator.tool_bridge.resolve_tools import ResolveTools

def build_fcpxml_from_edit(config: Any, edit: models.SemanticEdit) -> Path:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    # Use render_passes_used from state if available, but config doesn't have it.
    # We can just use a timestamp or overwrite?
    # The plan says `build_fcpxml_from_edit(config, edit)`.
    # I'll use a generic name or timestamp.
    import time
    ts = int(time.time())
    fcpxml_path = config.output_dir / f"{edit.project_name}_{ts}.fcpxml"
    
    return build_and_export_semantic_edit(edit, fcpxml_path)

def import_fcpxml_to_resolve(
    fcpxml_path: Path, 
    project_name: str, 
    timeline_name: str
) -> Dict[str, Any]:
    
    tools = ResolveTools(project_name=project_name)
    result = tools.import_fcpxml(fcpxml_path, timeline_name=timeline_name)
    return {"timeline_name": result}

def render_preview(
    project_name: str,
    timeline_name: str, 
    output_dir: Path,
    preset: str = "YouTube 1080p"
) -> Path:
    tools = ResolveTools(project_name=project_name)
    dest = output_dir / f"{timeline_name}_preview.mp4"
    
    return tools.render(dest, preset=preset)
