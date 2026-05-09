from typing import List, Any
from src.orchestrator import models
from src.orchestrator.clip_slices import ClipSlice, generate_candidate_slices
from src.orchestrator.tool_bridge.retrieval_tools import find_clips
from src.orchestrator.tools.project import get_shot_plan, get_script_segments

def propose_candidate_windows(
    beat_id: str,
    shot_id: str,
    state: Any, # EditorSessionState
    config: Any # EditorSessionConfig
) -> List[ClipSlice]:
    """
    High-level helper to find candidate slices for a specific shot.
    """
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
        
    # Build query
    query = f"{beat.visual_intent}. {shot.visual_goal}"
    if shot.keywords:
        query += " " + " ".join(shot.keywords)
        
    # Search
    constraints = {}
    if shot.preferred_assets:
        constraints["video_ids"] = shot.preferred_assets

    search_results = find_clips(query, limit=10, constraints=constraints)
        
    # Determine VO range duration
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
            
    if not found:
        # Fallback duration
        vo_range = models.TimeSpan(start=0, duration=2.0)
    else:
        vo_range = models.TimeSpan(start=start_time, duration=end_time - start_time)
        
    return generate_candidate_slices(
        beat=beat,
        shot=shot,
        vo_range=vo_range,
        search_results=search_results
    )
