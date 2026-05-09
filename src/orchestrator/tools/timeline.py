from pathlib import Path
from typing import List, Any, Optional, Dict
import json

from src.orchestrator import models
from src.orchestrator.clip_chooser import ClipChooser, ClipSliceDecision
from src.orchestrator.clip_slices import ClipSlice
from src.orchestrator.tools.project import get_shot_plan, get_script_segments

def choose_clip_for_shot(
    beat_id: str,
    shot_id: str,
    candidates: List[Any], # Can be dicts or ClipSlice objects
    state: Any, # EditorSessionState
    config: Any # EditorSessionConfig
) -> ClipSliceDecision:
    
    # Convert candidates to ClipSlice if they are dicts
    typed_candidates = []
    for c in candidates:
        if isinstance(c, dict):
            typed_candidates.append(ClipSlice(**c))
        else:
            typed_candidates.append(c)
            
    # Find Beat
    beat = next((b for b in state.beat_map.beats if b.id == beat_id), None)
    if not beat:
        raise ValueError(f"Beat {beat_id} not found")
        
    # Find ShotInstruction
    shot_plans_dir = config.output_dir / "shot_plans"
    shot_plan = get_shot_plan(shot_plans_dir, beat_id)
    if not shot_plan:
        raise ValueError(f"Shot plan for beat {beat_id} not found")
        
    shot = next((s for s in shot_plan.shot_instructions if s.shot_id == shot_id), None)
    if not shot:
        raise ValueError(f"Shot {shot_id} not found in plan for beat {beat_id}")
        
    # Find VO duration and excerpt
    segments = get_script_segments(config.script_alignment_path)
    
    start_time = float('inf')
    end_time = float('-inf')
    found = False
    
    for seg in segments:
        if seg["id"] in shot.narration_ids:
            t0 = float(seg.get("t0", 0.0))
            t1 = float(seg.get("t1", t0))
            start_time = min(start_time, t0)
            end_time = max(end_time, t1)
            found = True
            
    vo_duration = 2.0
    if found:
        vo_duration = end_time - start_time
        
    chooser = ClipChooser()
    decision = chooser.choose_clip(
        vo_excerpt=shot.vo_excerpt,
        vo_duration=vo_duration,
        coverage_note=shot.coverage_note,
        beat_intent=beat.visual_intent,
        required_events=shot.required_events,
        keywords=shot.keywords,
        candidates=typed_candidates
    )
    
    if not decision:
        raise ValueError(chooser.last_failure_reason or "No clip chosen")
        
    return decision

def get_semantic_edit_tool(config: Any) -> models.SemanticEdit:
    p = config.output_dir / "semantic_edit_agent.json"
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            return models.SemanticEdit(**json.load(f))
            
    return models.SemanticEdit(
        project_name=config.project_name,
        segments=[]
    )

def write_semantic_edit_tool(config: Any, edit: models.SemanticEdit) -> Path:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    p = config.output_dir / "semantic_edit_agent.json"
    with open(p, "w", encoding="utf-8") as f:
        f.write(edit.model_dump_json(indent=2))
    return p

def add_or_update_segment(state: Any, segment: Any) -> models.SemanticEdit:
    # Handle dict input
    if isinstance(segment, dict):
        segment = models.EditSegment(**segment)
        
    edit = state.semantic_edit
    # Check if exists
    for i, seg in enumerate(edit.segments):
        if seg.id == segment.id:
            edit.segments[i] = segment
            return edit
    
    edit.segments.append(segment)
    # Sort by start time if available
    edit.segments.sort(key=lambda s: s.narration_span.start if s.narration_span else float('inf'))
    return edit

def remove_segment(state: Any, segment_id: str) -> models.SemanticEdit:
    edit = state.semantic_edit
    edit.segments = [s for s in edit.segments if s.id != segment_id]
    return edit

def add_clip_to_segment(
    state: Any, 
    segment_id: str, 
    clip_spec: Any, 
    role: str = "video"
) -> models.SemanticEdit:
    
    # Handle dict input
    if isinstance(clip_spec, dict):
        clip_spec = models.ClipSpec(**clip_spec)
        
    edit = state.semantic_edit
    for seg in edit.segments:
        if seg.id == segment_id:
            if role == "primary":
                seg.primary_clip = clip_spec # Deprecated but supported
            elif role == "video":
                seg.video_clips.append(clip_spec)
            elif role == "broll":
                seg.broll.append(clip_spec)
            else:
                raise ValueError(f"Unknown role {role}")
            return edit
            
    raise ValueError(f"Segment {segment_id} not found")

def remove_clip_from_segment(state: Any, segment_id: str, asset_id: str) -> models.SemanticEdit:
    edit = state.semantic_edit
    for seg in edit.segments:
        if seg.id == segment_id:
            # Remove from all lists
            if seg.primary_clip and seg.primary_clip.asset_id == asset_id:
                seg.primary_clip = None
            
            seg.video_clips = [c for c in seg.video_clips if c.asset_id != asset_id]
            seg.broll = [c for c in seg.broll if c.asset_id != asset_id]
            return edit
            
    raise ValueError(f"Segment {segment_id} not found")

def update_narration_span(state: Any, segment_id: str, span: Any) -> models.SemanticEdit:
    if isinstance(span, dict):
        span = models.TimeSpan(**span)
        
    edit = state.semantic_edit
    for seg in edit.segments:
        if seg.id == segment_id:
            seg.narration_span = span
            return edit
    raise ValueError(f"Segment {segment_id} not found")
