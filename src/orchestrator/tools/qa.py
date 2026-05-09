from typing import Dict, Any, List
import json
from src.orchestrator import models
from src.orchestrator.tools.project import get_script_segments

def summarize_coverage_for_beat(state: Any, beat_id: str, config: Any) -> Dict[str, Any]:
    beat = next((b for b in state.beat_map.beats if b.id == beat_id), None)
    if not beat:
        raise ValueError(f"Beat {beat_id} not found")
        
    # Get script segments for this beat
    all_segments = get_script_segments(config.script_alignment_path)
    beat_segments = [s for s in all_segments if s["id"] in beat.segment_ids]
    
    # Check coverage in semantic edit
    covered_segments = []
    missing_segments = []
    
    total_vo_duration = 0.0
    total_clip_duration = 0.0
    
    for seg in beat_segments:
        seg_id = seg["id"]
        t0 = float(seg.get("t0", 0.0))
        t1 = float(seg.get("t1", t0))
        duration = t1 - t0
        total_vo_duration += duration
        
        edit_seg = next((s for s in state.semantic_edit.segments if s.id == seg_id), None)
        
        if edit_seg and (edit_seg.video_clips or edit_seg.primary_clip):
            covered_segments.append(seg_id)
            # Sum clip durations
            clips = edit_seg.video_clips + ([edit_seg.primary_clip] if edit_seg.primary_clip else [])
            for clip in clips:
                if clip.source_range:
                    total_clip_duration += clip.source_range.duration
        else:
            missing_segments.append(seg_id)
            
    return {
        "beat_id": beat_id,
        "total_vo_duration": total_vo_duration,
        "total_clip_duration": total_clip_duration,
        "coverage_ratio": total_clip_duration / total_vo_duration if total_vo_duration > 0 else 0,
        "covered_segments": covered_segments,
        "missing_segments": missing_segments
    }

def list_uncovered_vo_segments(state: Any, config: Any) -> List[str]:
    all_segments = get_script_segments(config.script_alignment_path)
    uncovered = []
    
    for seg in all_segments:
        seg_id = seg["id"]
        edit_seg = next((s for s in state.semantic_edit.segments if s.id == seg_id), None)
        if not edit_seg or (not edit_seg.video_clips and not edit_seg.primary_clip):
            uncovered.append(seg_id)
            
    return uncovered

def get_director_notes_tool(config: Any) -> models.DirectorNotesDocument:
    p = config.output_dir / "director_notes_agent.json"
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            return models.DirectorNotesDocument(**json.load(f))
            
    return models.DirectorNotesDocument(
        project_name=config.project_name,
        timeline_name=config.timeline_name
    )

def append_director_note(state: Any, entry: Any, config: Any = None):
    if isinstance(entry, dict):
        # Auto-fill missing VO data if config is available
        if config:
            # Load segments if we might need them
            all_segments = None
            
            # 1. Infer narration_ids from shot_id if missing
            if not entry.get("narration_ids") and entry.get("shot_id"):
                # Lazy load
                if all_segments is None:
                    all_segments = get_script_segments(config.script_alignment_path)
                
                # Check if shot_id matches a segment ID
                if any(s["id"] == entry["shot_id"] for s in all_segments):
                    entry["narration_ids"] = [entry["shot_id"]]

            # 2. Fill missing VO details
            if entry.get("narration_ids"):
                needs_fill = False
                if not entry.get("vo_excerpt"): needs_fill = True
                if not entry.get("vo_range") or (entry["vo_range"].get("duration", 0) == 0): needs_fill = True
                
                if needs_fill:
                    if all_segments is None:
                        all_segments = get_script_segments(config.script_alignment_path)
                        
                    # Find relevant segments
                    relevant = [s for s in all_segments if s["id"] in entry["narration_ids"]]
                    
                    if relevant:
                        # Sort by time
                        relevant.sort(key=lambda x: float(x.get("t0", 0)))
                        
                        # DEBUG: Print what we found
                        print(f"[QA] Found segments for {entry['narration_ids']}: {[s.get('id') for s in relevant]}")
                        print(f"[QA] Texts found: {[s.get('text') for s in relevant]}")

                        # ALWAYS overwrite excerpt and range from source of truth to ensure accuracy
                        text_parts = [s.get("text", "") for s in relevant]
                        entry["vo_excerpt"] = " ".join(text_parts)
                        
                        # Fill range
                        start = float(relevant[0].get("t0", 0))
                        end = float(relevant[-1].get("t1", start))
                        entry["vo_range"] = {
                            "start": start,
                            "duration": end - start
                        }
                    else:
                        # Debug logging if we can't find the segments
                        print(f"[QA] WARNING: Could not find narration segments for IDs: {entry['narration_ids']}")
                        print(f"[QA] Available IDs sample: {[s['id'] for s in all_segments[:5]]}")

        entry = models.DirectorNoteEntry(**entry)
    
    # Remove ANY existing entries with same beat_id and shot_id to ensure no duplicates
    # This cleans up stale duplicates as well if we touch them
    state.director_notes.entries = [e for e in state.director_notes.entries 
                                    if not (e.beat_id == entry.beat_id and e.shot_id == entry.shot_id)]
    
    state.director_notes.entries.append(entry)
