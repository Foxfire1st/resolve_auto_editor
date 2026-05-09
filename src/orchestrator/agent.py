"""
Simple Agent Orchestrator for Video Editing.

This module defines an Agent that can:
1. Plan an edit using OTIO tools.
2. Execute the edit in DaVinci Resolve via the Gateway API.
"""

import json
import os
import requests
from pathlib import Path
from typing import Any, List, Dict, Optional
from dotenv import load_dotenv

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from orchestrator.tool_bridge import otio_tools, retrieval_tools
from orchestrator.models import SemanticEdit, EditSegment, ClipSpec, SourceRange, TimeSpan

# Load environment variables
load_dotenv()

GATEWAY_URL = "http://localhost:8000"

class Clip(BaseModel):
    name: str = Field(..., description="Name of the clip file (without extension).")
    start: int = Field(..., description="Start frame in the source clip.")
    duration: int = Field(..., description="Duration in frames.")

class VideoEditingAgent:
    def __init__(self, project_name: str = "Test Project"):
        self.project_name = project_name
        self.conversation_history: List[Dict[str, str]] = []
        
        # Initialize Gemini Client
        api_key = os.environ.get("gemini_api_key")
        if not api_key:
            raise ValueError("gemini_api_key not found in environment variables")
            
        self.client = genai.Client(api_key=api_key)
        self.model_name = os.environ.get("GEMINI_MODEL_EDITOR", "gemini-1.5-pro-002")

    def get_resolve_state(self) -> Dict[str, Any]:
        """Observe the current state of Resolve."""
        try:
            resp = requests.get(f"{GATEWAY_URL}/resolve/timelines")
            timelines = resp.json()
            return {"timelines": timelines}
        except Exception as e:
            return {"error": str(e)}

    def create_edit(self, timeline_name: str, segments: List[EditSegment]):
        """
        Create a new video edit timeline from a list of segments.
        
        Args:
            timeline_name: The name of the new timeline.
            segments: List of edit segments (narration, primary clip, broll).
        """
        print(f"[Agent] Creating edit '{timeline_name}' with {len(segments)} segments...")
        
        edit_plan = SemanticEdit(
            project_name=timeline_name,
            fps=24.0,
            segments=segments
        )

        # 2. Build Plan & Export FCPXML (Local Action)
        temp_fcpxml = Path(f"C:/Temp/{timeline_name}.fcpxml")
        temp_fcpxml.parent.mkdir(parents=True, exist_ok=True)
        
        otio_tools.build_and_export_semantic_edit(
            edit=edit_plan,
            destination=temp_fcpxml
        )
        print(f"[Agent] Generated FCPXML at {temp_fcpxml}")

        # 3. Import into Resolve (Remote Action via API)
        print(f"[Agent] Importing to Resolve...")
        payload = {
            "fcpxml_path": str(temp_fcpxml),
            "timeline_name": timeline_name,
            "project_name": self.project_name
        }
        resp = requests.post(f"{GATEWAY_URL}/resolve/import_fcpxml", json=payload)
        
        if resp.status_code == 200:
            print(f"[Agent] Success! Timeline '{resp.json()}' created in Resolve.")
            return f"Created timeline '{timeline_name}' in Resolve."
        else:
            print(f"[Agent] Failed: {resp.text}")
            return f"Failed to import timeline: {resp.text}"

    def run(self, instruction: str):
        """Main loop."""
        # 1. Observe
        state = self.get_resolve_state()
        print(f"[Agent] Current State: {len(state.get('timelines', []))} timelines exist.")

        # 2. Reason (Call LLM)
        system_prompt = f"""You are a professional video editor assistant. 
        You have access to DaVinci Resolve via tools.
        Current project state: {json.dumps(state)}
        
        Your workflow:
        1. Search for clips using 'find_clips' if you need to find footage.
        2. Create an edit using 'create_edit'.
        
        When creating an edit, you must provide a list of 'EditSegment' objects.
        Each segment represents a beat in the video.
        - 'primary_clip': The main visual (A-Roll).
        - 'broll': Overlay clips (B-Roll).
        - 'narration_span': Timing for the segment.
        
        Use the 'asset_id' returned by 'find_clips' for the clip specs.
        """
        
        print(f"\n[LLM Thinking] User: {instruction}")
        
        response = self.client.models.generate_content(
            model=self.model_name,
            contents=[instruction],
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=[self.create_edit, retrieval_tools.find_clips],
                temperature=0
            )
        )

        # 3. Act (Parse Tool Call)
        # Check if the model wants to call a function
        if response.function_calls:
            for call in response.function_calls:
                print(f"[Agent] Tool Call: {call.name}")
                if call.name == "find_clips":
                    args = call.args
                    query = args.get("query")
                    limit = int(args.get("limit", 5))
                    results = retrieval_tools.find_clips(query, limit)
                    print(f"[Agent] Found {len(results)} clips.")
                    
                    tool_output = f"Found clips: {json.dumps([r.model_dump() for r in results])}"
                    print(f"[Agent] Tool Output: {tool_output[:200]}...")
                    
                    # Continue conversation
                    response2 = self.client.models.generate_content(
                        model=self.model_name,
                        contents=[
                            types.Content(role="user", parts=[types.Part(text=instruction)]),
                            types.Content(role="model", parts=[types.Part(function_call=call)]),
                            types.Content(role="tool", parts=[types.Part(function_response=types.FunctionResponse(name="find_clips", response={"result": tool_output}))])
                        ],
                        config=types.GenerateContentConfig(
                            system_instruction=system_prompt,
                            tools=[self.create_edit, retrieval_tools.find_clips],
                            temperature=0
                        )
                    )
                    
                    if response2.function_calls:
                        for call2 in response2.function_calls:
                            if call2.name == "create_edit":
                                self._handle_create_edit(call2)
                    else:
                         print(f"[Agent] LLM replied: {response2.text}")

                elif call.name == "create_edit":
                    self._handle_create_edit(call)
        else:
            print(f"[Agent] LLM replied with text: {response.text}")

    def _handle_create_edit(self, call):
        args = call.args
        timeline_name = args.get("timeline_name")
        segments_data = args.get("segments")
        
        # Convert to Pydantic models
        segments = []
        if segments_data:
            for s in segments_data:
                # Handle nested objects
                primary = s.get("primary_clip")
                if primary:
                    primary = ClipSpec(**primary)
                
                broll = []
                if s.get("broll"):
                    broll = [ClipSpec(**b) for b in s.get("broll")]
                
                narration = s.get("narration_span")
                if narration:
                    narration = TimeSpan(**narration)

                segments.append(EditSegment(
                    id=s.get("id", "seg"),
                    primary_clip=primary,
                    broll=broll,
                    narration_span=narration
                ))
                
        result = self.create_edit(timeline_name=timeline_name, segments=segments)
        print(f"[Agent] Result: {result}")

if __name__ == "__main__":
    # Example usage
    agent = VideoEditingAgent()
    agent.run("Find relevant clips for the requested moment and create a timeline with the best match.")
