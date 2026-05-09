import json
from pathlib import Path
from typing import List, Dict, Optional, Any
from dataclasses import dataclass

@dataclass
class EventContext:
    time_s: float
    description: str
    labels: List[str]

def locate_meta_json(video_path: str) -> Optional[Path]:
    """
    Locates the corresponding Gemini metadata JSON for a video file.
    Assumes structure: .../folder/video.mp4 -> .../folder/meta/video_gemini.json
    """
    vid_path = Path(video_path)
    # Try 'meta' subdirectory first (standard for this project)
    meta_path = vid_path.parent / "meta" / f"{vid_path.stem}_gemini.json"
    if meta_path.exists():
        return meta_path
    
    # Try same directory
    sibling_path = vid_path.parent / f"{vid_path.stem}_gemini.json"
    if sibling_path.exists():
        return sibling_path
        
    return None

def get_video_context(video_path: str, timestamp: float, window_s: float = 10.0) -> List[Dict[str, Any]]:
    """
    Retrieves semantic events from the metadata JSON around a specific timestamp.
    
    Args:
        video_path: Path to the video file.
        timestamp: The center timestamp to look around.
        window_s: The time window (plus/minus) to retrieve events for.
        
    Returns:
        List of event dictionaries.
    """
    json_path = locate_meta_json(video_path)
    if not json_path:
        return []
        
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            
        events = data.get("events", [])
        start_window = timestamp - window_s
        end_window = timestamp + window_s
        
        relevant_events = []
        for event in events:
            t = event.get("time_s", 0.0)
            if start_window <= t <= end_window:
                relevant_events.append(event)
                
        return relevant_events
    except Exception as e:
        print(f"Error reading metadata for {video_path}: {e}")
        return []
