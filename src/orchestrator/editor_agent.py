import json
import logging
import inspect
import os
from pathlib import Path
from typing import List, Optional, Dict, Any

from pydantic import BaseModel, Field
from google import genai
from google.genai import types

from src.orchestrator import models
from src.orchestrator.core_editor import load_beat_map, load_segment_index
from src.orchestrator.tools import project, footage, slicing, timeline, qa, resolve, review
from src.orchestrator.clip_slices import make_slices

LOGGER = logging.getLogger(__name__)

TOOL_REGISTRY = {
    # Project
    "get_script_segments": project.get_script_segments,
    "get_beat_map": project.get_beat_map_tool,
    "update_beat": project.update_beat,
    "split_beat": project.split_beat,
    "merge_beats": project.merge_beats,
    "get_shot_plan": project.get_shot_plan,
    "set_shot_plan": project.set_shot_plan,
    
    # Footage
    "list_assets": footage.list_assets,
    "summarize_asset": footage.summarize_asset,
    "search_segments": footage.search_segments,
    "inspect_window": footage.inspect_window,
    
    # Slicing
    "propose_candidate_windows": slicing.propose_candidate_windows,
    "make_slices": make_slices,
    
    # Timeline
    "choose_clip_for_shot": timeline.choose_clip_for_shot,
    "get_semantic_edit": timeline.get_semantic_edit_tool,
    "add_or_update_segment": timeline.add_or_update_segment,
    "remove_segment": timeline.remove_segment,
    "add_clip_to_segment": timeline.add_clip_to_segment,
    "remove_clip_from_segment": timeline.remove_clip_from_segment,
    "update_narration_span": timeline.update_narration_span,
    
    # QA
    "summarize_coverage_for_beat": qa.summarize_coverage_for_beat,
    "list_uncovered_vo_segments": qa.list_uncovered_vo_segments,
    "get_director_notes": qa.get_director_notes_tool,
    "append_director_note": qa.append_director_note,
    
    # Resolve
    "build_fcpxml_from_edit": resolve.build_fcpxml_from_edit,
    "import_fcpxml_to_resolve": resolve.import_fcpxml_to_resolve,
    "render_preview": resolve.render_preview,
    
    # Review
    "upload_video_for_review": review.upload_video_for_review,
    "summarize_video": review.summarize_video,
}

class EditorSessionConfig(BaseModel):
    """Configuration for an Editor Agent session."""
    project_name: str
    timeline_name: str
    profile_name: Optional[str] = None
    script_alignment_path: Path
    beat_map_path: Path
    output_dir: Path
    max_passes: int = 3

class EditorSessionState(BaseModel):
    """Mutable state of the editing session."""
    beat_map: models.BeatMap
    semantic_edit: models.SemanticEdit
    director_notes: models.DirectorNotesDocument
    working_log: List[str] = Field(default_factory=list)
    render_passes_used: int = 0

class EditorAgent:
    """
    The autonomous agent responsible for orchestrating the editing process.
    """

    def __init__(self, config: EditorSessionConfig):
        self.config = config
        self.state = self._initialize_state()
        
        api_key = (
            os.environ.get("gemini_api_key")
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
        )
        if not api_key:
            LOGGER.warning("Gemini API key not found. Agent will fail if LLM is needed.")
            self.client = None
        else:
            self.client = genai.Client(api_key=api_key)
            
        self.model_name = os.environ.get("GEMINI_MODEL_EDITOR", "gemini-2.5-flash")

    def _initialize_state(self) -> EditorSessionState:
        """Loads initial state from disk or creates empty structures."""
        
        # Load Beat Map
        if self.config.beat_map_path.exists():
            beat_map = load_beat_map(self.config.beat_map_path)
        else:
            LOGGER.warning(f"Beat map not found at {self.config.beat_map_path}, creating empty.")
            beat_map = models.BeatMap(beats=[])

        # Initialize Semantic Edit
        edit_path = self.config.output_dir / "semantic_edit_agent.json"
        if edit_path.exists():
            with open(edit_path, "r", encoding="utf-8") as f:
                semantic_edit = models.SemanticEdit(**json.load(f))
        else:
            semantic_edit = models.SemanticEdit(
                project_name=self.config.project_name,
                segments=[]
            )

        # Initialize Director Notes
        notes_path = self.config.output_dir / "director_notes_agent.json"
        if notes_path.exists():
            with open(notes_path, "r", encoding="utf-8") as f:
                director_notes = models.DirectorNotesDocument(**json.load(f))
        else:
            director_notes = models.DirectorNotesDocument(
                project_name=self.config.project_name,
                timeline_name=self.config.timeline_name
            )

        return EditorSessionState(
            beat_map=beat_map,
            semantic_edit=semantic_edit,
            director_notes=director_notes
        )

    def save_state(self):
        """Persists the current state to the output directory."""
        self.config.output_dir.mkdir(parents=True, exist_ok=True)

        # Save Beat Map
        beat_map_out = self.config.output_dir / "beat_map_agent.json"
        with open(beat_map_out, "w", encoding="utf-8") as f:
            f.write(self.state.beat_map.model_dump_json(indent=2))

        # Save Semantic Edit
        edit_out = self.config.output_dir / "semantic_edit_agent.json"
        with open(edit_out, "w", encoding="utf-8") as f:
            f.write(self.state.semantic_edit.model_dump_json(indent=2))

        # Save Director Notes
        notes_out = self.config.output_dir / "director_notes_agent.json"
        with open(notes_out, "w", encoding="utf-8") as f:
            f.write(self.state.director_notes.model_dump_json(indent=2))
        
        LOGGER.info(f"Session state saved to {self.config.output_dir}")

    def invoke_tool(self, name: str, args: dict) -> Any:
        tool_func = TOOL_REGISTRY.get(name)
        if not tool_func:
            raise ValueError(f"Unknown tool: {name}")
            
        sig = inspect.signature(tool_func)
        call_args = {}
        
        for param_name, param in sig.parameters.items():
            if param_name == "state":
                call_args["state"] = self.state
            elif param_name == "config":
                call_args["config"] = self.config
            elif param_name == "script_alignment_path":
                call_args["script_alignment_path"] = self.config.script_alignment_path
            elif param_name == "beat_map_path":
                call_args["beat_map_path"] = self.config.beat_map_path
            elif param_name == "shot_plans_dir":
                call_args["shot_plans_dir"] = self.config.output_dir / "shot_plans"
            elif param_name == "profile":
                call_args["profile"] = self.config.profile_name or "default"
            elif param_name in args:
                call_args[param_name] = args[param_name]
            elif param.default != inspect.Parameter.empty:
                continue 
            else:
                pass
                
        try:
            return tool_func(**call_args)
        except Exception as e:
            LOGGER.error(f"Tool execution failed: {name} with args {args}. Error: {e}")
            raise e

    def _get_tool_definitions(self) -> List[types.Tool]:
        """
        Returns the list of tool definitions for Gemini.
        We manually define these to exclude 'state' and 'config' from the schema.
        """
        
        # Helper to create function declarations
        def decl(name, description, parameters):
            return types.FunctionDeclaration(
                name=name,
                description=description,
                parameters=parameters
            )

        return [types.Tool(function_declarations=[
            # Project
            decl(
                name="get_script_segments",
                description="Returns list of narration segments with IDs, text, and timing. Can filter by beat.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "beat_id": types.Schema(type=types.Type.STRING, description="Optional beat ID to filter segments")
                    },
                    required=[]
                )
            ),
            decl(
                name="get_beat_map",
                description="Returns the current BeatMap (beats with segment_ids, visual_intent, pacing, etc.).",
                parameters=types.Schema(type=types.Type.OBJECT, properties={})
            ),
            decl(
                name="get_shot_plan",
                description="Returns the ShotPlan for a beat if it exists.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={"beat_id": types.Schema(type=types.Type.STRING)},
                    required=["beat_id"]
                )
            ),
            
            # Footage
            decl(
                name="list_assets",
                description="Returns a page of clips with basic metadata.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "profile": types.Schema(type=types.Type.STRING),
                        "page": types.Schema(type=types.Type.INTEGER),
                        "page_size": types.Schema(type=types.Type.INTEGER)
                    },
                    required=["profile"]
                )
            ),
            decl(
                name="summarize_asset",
                description="Summarize the entire clip: main events, key abilities, characters.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={"asset_id": types.Schema(type=types.Type.STRING)},
                    required=["asset_id"]
                )
            ),
            decl(
                name="search_segments",
                description="Search for footage segments.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "query": types.Schema(type=types.Type.STRING),
                        "constraints": types.Schema(type=types.Type.OBJECT)
                    },
                    required=["query"]
                )
            ),
            decl(
                name="inspect_window",
                description="Fetches caption snippets, tags, and events for a specific window.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "asset_id": types.Schema(type=types.Type.STRING),
                        "start": types.Schema(type=types.Type.NUMBER),
                        "end": types.Schema(type=types.Type.NUMBER)
                    },
                    required=["asset_id", "start", "end"]
                )
            ),
            
            # Slicing
            decl(
                name="propose_candidate_windows",
                description="Find candidate clip slices for a specific shot.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "beat_id": types.Schema(type=types.Type.STRING),
                        "shot_id": types.Schema(type=types.Type.STRING)
                    },
                    required=["beat_id", "shot_id"]
                )
            ),
            decl(
                name="make_slices",
                description="Propose slices for specific windows in an asset.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "asset_id": types.Schema(type=types.Type.STRING),
                        "windows": types.Schema(
                            type=types.Type.ARRAY,
                            items=types.Schema(
                                type=types.Type.OBJECT,
                                properties={
                                    "start": types.Schema(type=types.Type.NUMBER),
                                    "end": types.Schema(type=types.Type.NUMBER)
                                }
                            )
                        ),
                        "desired_duration": types.Schema(type=types.Type.NUMBER)
                    },
                    required=["asset_id", "windows", "desired_duration"]
                )
            ),
            
            # Timeline
            decl(
                name="choose_clip_for_shot",
                description="Choose the best clip from candidates for a shot.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "beat_id": types.Schema(type=types.Type.STRING),
                        "shot_id": types.Schema(type=types.Type.STRING),
                        "candidates": types.Schema(
                            type=types.Type.ARRAY,
                            items=types.Schema(type=types.Type.OBJECT) # Simplified for now
                        )
                    },
                    required=["beat_id", "shot_id", "candidates"]
                )
            ),
            decl(
                name="add_or_update_segment",
                description="Add or replace an EditSegment in the semantic edit.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "segment": types.Schema(type=types.Type.OBJECT) # Expects EditSegment dict
                    },
                    required=["segment"]
                )
            ),
            decl(
                name="add_clip_to_segment",
                description="Insert a clip into a segment.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "segment_id": types.Schema(type=types.Type.STRING),
                        "clip_spec": types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "asset_id": types.Schema(type=types.Type.STRING, description="The video_id or filename"),
                                "source_range": types.Schema(
                                    type=types.Type.OBJECT,
                                    properties={
                                        "start": types.Schema(type=types.Type.NUMBER),
                                        "duration": types.Schema(type=types.Type.NUMBER)
                                    }
                                ),
                                "speed": types.Schema(type=types.Type.NUMBER)
                            },
                            required=["asset_id"]
                        ),
                        "role": types.Schema(type=types.Type.STRING)
                    },
                    required=["segment_id", "clip_spec"]
                )
            ),
            
            # QA
            decl(
                name="append_director_note",
                description="Log a decision in the director notes.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "entry": types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "beat_id": types.Schema(type=types.Type.STRING),
                                "shot_id": types.Schema(type=types.Type.STRING),
                                "narration_ids": types.Schema(type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING)),
                                "vo_excerpt": types.Schema(type=types.Type.STRING),
                                "vo_range": types.Schema(
                                    type=types.Type.OBJECT,
                                    properties={
                                        "start": types.Schema(type=types.Type.NUMBER),
                                        "duration": types.Schema(type=types.Type.NUMBER)
                                    }
                                ),
                                "clip_asset_id": types.Schema(type=types.Type.STRING),
                                "clip_range": types.Schema(
                                    type=types.Type.OBJECT,
                                    properties={
                                        "start": types.Schema(type=types.Type.NUMBER),
                                        "duration": types.Schema(type=types.Type.NUMBER)
                                    }
                                ),
                                "visual_summary": types.Schema(type=types.Type.STRING),
                                "coverage_note": types.Schema(type=types.Type.STRING),
                                "reasoning": types.Schema(type=types.Type.STRING)
                            },
                            required=["beat_id", "shot_id", "narration_ids", "vo_excerpt", "vo_range", "clip_asset_id", "clip_range", "visual_summary", "coverage_note", "reasoning"]
                        )
                    },
                    required=["entry"]
                )
            ),
            
            # Resolve
            decl(
                name="build_fcpxml_from_edit",
                description="Export the semantic edit to FCPXML.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "edit": types.Schema(type=types.Type.OBJECT) # Expects SemanticEdit dict
                    },
                    required=["edit"]
                )
            ),
            decl(
                name="import_fcpxml_to_resolve",
                description="Import FCPXML into Resolve.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "fcpxml_path": types.Schema(type=types.Type.STRING),
                        "project_name": types.Schema(type=types.Type.STRING),
                        "timeline_name": types.Schema(type=types.Type.STRING)
                    },
                    required=["fcpxml_path", "project_name", "timeline_name"]
                )
            ),
            decl(
                name="render_preview",
                description="Render a preview of the timeline.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "project_name": types.Schema(type=types.Type.STRING),
                        "timeline_name": types.Schema(type=types.Type.STRING),
                        "output_dir": types.Schema(type=types.Type.STRING),
                        "preset": types.Schema(type=types.Type.STRING)
                    },
                    required=["project_name", "timeline_name", "output_dir"]
                )
            ),
            
            # Review
            decl(
                name="upload_video_for_review",
                description="Upload a video file for review.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "path": types.Schema(type=types.Type.STRING)
                    },
                    required=["path"]
                )
            ),
            decl(
                name="summarize_video",
                description="Ask Gemini to watch and critique the video.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "file_name": types.Schema(type=types.Type.STRING),
                        "expectations": types.Schema(type=types.Type.STRING)
                    },
                    required=["file_name", "expectations"]
                )
            )
        ])]

    def _run_agent_loop(self, system_prompt: str, initial_message: str, max_steps: int):
        """
        Generic agent loop that handles tool calls and conversation.
        """
        if not self.client:
            raise RuntimeError("Gemini client not initialized")

        chat = self.client.chats.create(
            model=self.model_name,
            config=types.GenerateContentConfig(
                tools=self._get_tool_definitions(),
                system_instruction=system_prompt,
                temperature=0.1
            )
        )

        LOGGER.info(f"Starting agent loop")
        
        # Initial trigger
        response = chat.send_message(initial_message)
        
        step = 0
        while step < max_steps:
            step += 1
            
            # Check for tool calls
            if response.candidates and response.candidates[0].content.parts:
                parts = response.candidates[0].content.parts
                tool_calls = [p.function_call for p in parts if p.function_call]
                
                if tool_calls:
                    tool_outputs = []
                    for call in tool_calls:
                        # Convert args to dict
                        args = {k: v for k, v in call.args.items()}
                        LOGGER.info(f"Agent calling tool: {call.name} with args: {args}")
                        try:
                            result = self.invoke_tool(call.name, args)
                            
                            # Serialize result for LLM
                            if isinstance(result, BaseModel):
                                result_data = result.model_dump()
                            else:
                                result_data = result
                                
                            tool_outputs.append(
                                types.Part(
                                    function_response=types.FunctionResponse(
                                        name=call.name,
                                        response={"result": result_data}
                                    )
                                )
                            )
                        except Exception as e:
                            LOGGER.error(f"Tool error: {e}")
                            tool_outputs.append(
                                types.Part(
                                    function_response=types.FunctionResponse(
                                        name=call.name,
                                        response={"error": str(e)}
                                    )
                                )
                            )
                    
                    # Send tool outputs back
                    response = chat.send_message(tool_outputs)
                else:
                    # No tool calls, just text.
                    text = response.text
                    LOGGER.info(f"Agent response: {text}")
                    if "TASK_COMPLETE" in text:
                        break
                    
                    # If it didn't call a tool and didn't say complete, we can prompt it to continue.
                    response = chat.send_message("Proceed.")
            else:
                break
        
        self.save_state()

    def run_single_beat(self, beat_id: str, max_steps: int = 25):
        """
        Runs the agent loop for a single beat.
        """
        # Find the beat to provide context
        beat = next((b for b in self.state.beat_map.beats if b.id == beat_id), None)
        if not beat:
            raise ValueError(f"Beat {beat_id} not found")

        # CLEANUP: Remove existing notes and reset segments for this beat to ensure a fresh run
        LOGGER.info(f"Clearing previous state for beat {beat_id}...")
        
        # 1. Clear Director Notes
        self.state.director_notes.entries = [
            e for e in self.state.director_notes.entries 
            if e.beat_id != beat_id
        ]
        
        # 2. Reset Semantic Edit Segments (remove clips)
        for seg_id in beat.segment_ids:
            for seg in self.state.semantic_edit.segments:
                if seg.id == seg_id:
                    seg.video_clips = []
                    seg.broll = []
                    seg.primary_clip = None
                    # We keep narration info and ID
        
        self.save_state()

        system_prompt = f"""
You are an expert Video Editor Agent. Your goal is to assign clips to Beat {beat_id}.

Project: {self.config.project_name}
Beat Intent: {beat.visual_intent}
Beat Pacing: {beat.pacing}
Segments: {beat.segment_ids}

Your task:
1. Inspect the shot plan for this beat (use `get_shot_plan`).
2. If the shot plan is missing, create one using `set_shot_plan`.

3. EXECUTE THE PLAN:
   For each **Shot** in the plan:
   a. Find candidate clips using `propose_candidate_windows` or `search_segments`.
   b. Choose the best clip.
   c. **Apply to Timeline**: For EACH segment ID listed in `shot.narration_ids`:
      - Add the chosen clip to that segment using `add_clip_to_segment`.
   d. **Log Decision**: Create ONE director note for this shot using `append_director_note`.
      - Ensure `narration_ids` includes ALL segments covered by this shot.

4. FILL GAPS:
   Check if any segments in {beat.segment_ids} were NOT covered by the plan.
   For EACH missing segment:
   a. Create a new unique shot ID (e.g. "{beat.id}_L0xx").
   b. Find and choose a clip.
   c. Add to segment (`add_clip_to_segment`).
   d. Log decision (`append_director_note`).

5. VERIFICATION:
   - Use `summarize_coverage_for_beat` to confirm 100% coverage.
   - If `missing_segments` is not empty, fix them.
   - Do NOT say "TASK_COMPLETE" until coverage is 100%.

You have access to tools to query the script, footage, and modify the timeline.
Do not hallucinate asset IDs. Only use assets returned by search or list tools.
When finished, say "TASK_COMPLETE".
        """
        
        self._run_agent_loop(system_prompt, f"Please proceed with assigning clips for Beat {beat_id}.", max_steps)
        LOGGER.info(f"Agent loop finished for beat {beat_id}")

    def run_section(self, beat_ids: List[str], max_steps: int = 20):
        """
        Runs the agent loop for a section of beats (planning + execution).
        """
        system_prompt = f"""
You are an expert Video Editor Agent. Your goal is to plan and edit a section of the video corresponding to beats: {beat_ids}.

Project: {self.config.project_name}

Your task:
1. Inspect the script segments and existing beats for this section.
2. Inspect available footage using `list_assets` and `summarize_asset` to understand what visuals are available.
3. Refine the beats if necessary using `update_beat`, `split_beat`, or `merge_beats`. Ensure beats match available footage.
4. For each beat:
   a. Create or update the Shot Plan using `set_shot_plan`.
   b. Assign clips to shots using retrieval and timeline tools.
   c. Log decisions in director notes.

Do not hallucinate asset IDs.
When finished with the section, say "TASK_COMPLETE".
        """
        
        self._run_agent_loop(system_prompt, f"Please proceed with planning and editing for beats {beat_ids}.", max_steps)
        LOGGER.info(f"Agent loop finished for section {beat_ids}")

    def run_review_pass(self, max_steps: int = 20):
        """
        Builds, renders, and reviews the current edit.
        """
        system_prompt = f"""
You are an expert Video Editor Agent. Your goal is to review the current edit and fix issues.

Project: {self.config.project_name}
Timeline: {self.config.timeline_name}

Your task:
1. Build the FCPXML from the current semantic edit (`build_fcpxml_from_edit`).
2. Import it to Resolve (`import_fcpxml_to_resolve`).
3. Render a preview (`render_preview`).
4. Upload the preview for review (`upload_video_for_review`).
5. Ask for a summary/critique (`summarize_video`).
6. Based on the critique, fix any issues in the semantic edit (using timeline tools).
7. If major fixes were made, you might want to re-render (but be careful of loops).

When satisfied, say "TASK_COMPLETE".
        """
        
        self._run_agent_loop(system_prompt, "Please proceed with the visual review.", max_steps)
        self.state.render_passes_used += 1
        self.save_state()
        LOGGER.info("Review pass finished")

    def run_all_beats(self, max_steps: int = 50):
        """
        Runs the agent loop for all beats in the project.
        """
        beat_ids = [b.id for b in self.state.beat_map.beats]
        
        system_prompt = f"""
You are an expert Video Editor Agent. Your goal is to complete the edit for the entire project.

Project: {self.config.project_name}
Total Beats: {len(beat_ids)}

Your task:
1. Iterate through all beats.
2. Ensure each beat has a valid Shot Plan.
3. Assign clips to all shots.
4. Use `summarize_coverage_for_beat` and `list_uncovered_vo_segments` to check your work.
5. Fix any gaps.

When finished, say "TASK_COMPLETE".
        """
        
        self._run_agent_loop(system_prompt, "Please proceed with the full edit.", max_steps)
        LOGGER.info("Agent loop finished for all beats")

    def run_project(self):
        """High-level entrypoint to run the full project workflow."""
        self.run_all_beats()
