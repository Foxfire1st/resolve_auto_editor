import os
import sys
import json
import logging
import argparse
from pathlib import Path

# Add src to path
sys.path.append(str(Path(__file__).parent.parent))

from src.orchestrator.editor_agent import EditorAgent, EditorSessionConfig
from src.orchestrator.models import BeatMap, SemanticEdit, EditSegment, TimeSpan

logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger(__name__)

def load_json(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def main():
    parser = argparse.ArgumentParser(description="Run the AI Editor Agent")
    parser.add_argument("--project", required=True, help="Path to project directory")
    parser.add_argument("--beat", help="Beat ID to run (optional)")
    parser.add_argument("--section", nargs="+", help="List of beat IDs to run as a section")
    parser.add_argument("--all", action="store_true", help="Run all beats")
    parser.add_argument("--review", action="store_true", help="Run review pass")
    parser.add_argument("--max-steps", type=int, default=30, help="Max steps per loop")
    args = parser.parse_args()

    project_root = Path(args.project)
    if not project_root.exists():
        LOGGER.error(f"Project directory not found: {project_root}")
        return
    
    # Load inputs
    beat_map_path = project_root / "script_breakdown.json"
    alignment_path = project_root / "alignment_segments.json"
    
    if not beat_map_path.exists():
        LOGGER.error(f"Beat map not found at {beat_map_path}")
        return

    LOGGER.info("Loading beat map...")
    beat_map_data = load_json(beat_map_path)
    beat_map = BeatMap(**beat_map_data)
    
    LOGGER.info("Loading alignment segments...")
    # Prefer full alignment if available, else segments
    if (project_root / "alignment_full.json").exists():
        alignment_path = project_root / "alignment_full.json"
        LOGGER.info(f"Using full alignment from {alignment_path}")
    elif (project_root / "alignment_segments.json").exists():
        alignment_path = project_root / "alignment_segments.json"
        LOGGER.info(f"Using segment alignment from {alignment_path}")
    else:
        LOGGER.warning("No alignment file found!")
        alignment_path = project_root / "alignment_segments.json" # Default fallback

    edit_segments = []
    if alignment_path.exists():
        alignment_data = load_json(alignment_path)
        # alignment_data is {"segments": [...]}
        raw_segments = alignment_data.get("segments", [])
        
        for item in raw_segments:
            start = item.get("t0", 0.0)
            end = item.get("t1", 0.0)
            duration = end - start
            
            seg = EditSegment(
                id=item["id"],
                narration_span=TimeSpan(start=start, duration=duration)
            )
            edit_segments.append(seg)
    else:
        LOGGER.warning("Alignment file not found. Creating empty segments based on beat map.")
        # Fallback: create segments referenced in beats
        for beat in beat_map.beats:
            for seg_id in beat.segment_ids:
                edit_segments.append(EditSegment(id=seg_id))

    # Create initial SemanticEdit
    # TODO: Load existing semantic edit if available to resume work
    semantic_edit_path = project_root / "outputs" / "semantic_edit_agent.json"
    if semantic_edit_path.exists():
        LOGGER.info("Loading existing semantic edit...")
        semantic_edit = SemanticEdit(**load_json(semantic_edit_path))
    else:
        semantic_edit = SemanticEdit(
            project_name=project_root.name,
            fps=30.0, # Assumption
            segments=edit_segments
        )

    # Config
    config = EditorSessionConfig(
        project_name=project_root.name,
        timeline_name="Agent_Edit_v1",
        script_alignment_path=alignment_path,
        beat_map_path=beat_map_path,
        output_dir=project_root / "outputs"
    )
    
    LOGGER.info("Initializing EditorAgent...")
    try:
        agent = EditorAgent(config)
    except Exception as e:
        LOGGER.error(f"Failed to initialize agent: {e}")
        return
    
    # Hydrate state
    agent.state.beat_map = beat_map
    agent.state.semantic_edit = semantic_edit
    
    # Dispatch command
    try:
        if args.beat:
            LOGGER.info(f"Running for beat {args.beat}")
            agent.run_single_beat(args.beat, max_steps=args.max_steps)
        elif args.section:
            LOGGER.info(f"Running for section {args.section}")
            agent.run_section(args.section, max_steps=args.max_steps)
        elif args.all:
            LOGGER.info("Running for all beats")
            agent.run_all_beats(max_steps=args.max_steps)
        elif args.review:
            LOGGER.info("Running review pass")
            agent.run_review_pass(max_steps=args.max_steps)
        else:
            LOGGER.warning("No action specified. Use --beat, --section, --all, or --review.")
    except Exception as e:
        LOGGER.error(f"Error during execution: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
