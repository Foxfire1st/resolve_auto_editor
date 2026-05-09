from pathlib import Path
from typing import List, Dict, Any, Optional
import json

from src.orchestrator import models
from src.orchestrator.core_editor import load_segment_index, load_beat_map

def get_script_segments(script_alignment_path: Path, beat_map_path: Path = None, beat_id: str = None) -> List[Dict[str, Any]]:
    """
    Read the alignment JSON and return list of segments.
    If beat_id is provided, filters to only segments in that beat.
    """
    lookup = load_segment_index(script_alignment_path)
    
    # If filtering by beat
    if beat_id and beat_map_path:
        beat_map = load_beat_map(beat_map_path)
        target_beat = next((b for b in beat_map.beats if b.id == beat_id), None)
        if not target_beat:
            raise ValueError(f"Beat {beat_id} not found in map")
            
        # Filter segments
        segments = []
        for seg_id in target_beat.segment_ids:
            if seg_id in lookup:
                segments.append(lookup[seg_id])
            else:
                # Fallback if segment ID missing in alignment but present in beat
                segments.append({"id": seg_id, "text": "[Missing Segment Data]", "t0": 0, "t1": 0})
    else:
        # Return all, sorted by start time
        segments = list(lookup.values())
        segments.sort(key=lambda x: float(x.get("t0", 0.0)))
        
    return segments

def get_beat_map_tool(beat_map_path: Path) -> models.BeatMap:
    return load_beat_map(beat_map_path)

def write_beat_map_tool(beat_map_path: Path, beat_map: models.BeatMap) -> Path:
    with open(beat_map_path, "w", encoding="utf-8") as f:
        f.write(beat_map.model_dump_json(indent=2))
    return beat_map_path

def update_beat(beat_map: models.BeatMap, beat_id: str, changes: Dict[str, Any]) -> models.BeatMap:
    for i, beat in enumerate(beat_map.beats):
        if beat.id == beat_id:
            # Apply changes
            updated_data = beat.model_dump()
            updated_data.update(changes)
            # Validate by re-creating model
            new_beat = models.Beat(**updated_data)
            # Replace in place
            beat_map.beats[i] = new_beat
            return beat_map
    raise ValueError(f"Beat {beat_id} not found")

def split_beat(beat_map: models.BeatMap, beat_id: str, new_beats_spec: List[Dict[str, Any]]) -> models.BeatMap:
    # Find index
    idx = -1
    for i, b in enumerate(beat_map.beats):
        if b.id == beat_id:
            idx = i
            break
    
    if idx == -1:
        raise ValueError(f"Beat {beat_id} not found")
    
    new_beats = [models.Beat(**spec) for spec in new_beats_spec]
    
    # Replace
    beat_map.beats[idx:idx+1] = new_beats
    return beat_map

def merge_beats(beat_map: models.BeatMap, beat_ids: List[str], merged_spec: Dict[str, Any]) -> models.BeatMap:
    # Verify all exist
    indices = []
    for bid in beat_ids:
        found = False
        for i, b in enumerate(beat_map.beats):
            if b.id == bid:
                indices.append(i)
                found = True
                break
        if not found:
            raise ValueError(f"Beat {bid} not found")
            
    # Sort indices to remove correctly
    indices.sort()
    
    first_idx = indices[0]
    
    # Remove in reverse order to not mess up indices
    for i in reversed(indices):
        beat_map.beats.pop(i)
        
    new_beat = models.Beat(**merged_spec)
    beat_map.beats.insert(first_idx, new_beat)
    
    return beat_map

def get_shot_plan(shot_plans_dir: Path, beat_id: str) -> Optional[models.ShotPlan]:
    p = shot_plans_dir / f"shot_plan_{beat_id}.json"
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            return models.ShotPlan(**json.load(f))
    return None

def set_shot_plan(shot_plans_dir: Path, beat_id: str, plan: Any):
    if isinstance(plan, dict):
        plan = models.ShotPlan(**plan)
    shot_plans_dir.mkdir(parents=True, exist_ok=True)
    p = shot_plans_dir / f"shot_plan_{beat_id}.json"
    with open(p, "w", encoding="utf-8") as f:
        f.write(plan.model_dump_json(indent=2))
