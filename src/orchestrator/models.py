"""
Pydantic models for the Semantic Edit Schema.
These define the interface between the LLM Orchestrator and the OTIO Builder.
"""

from typing import List, Optional, Literal
from pydantic import BaseModel, Field

class TimeSpan(BaseModel):
    """A time range in seconds."""
    start: float = Field(..., description="Start time in seconds")
    duration: float = Field(..., description="Duration in seconds")

    @property
    def end(self) -> float:
        return self.start + self.duration

class SourceRange(BaseModel):
    """A range within a source media file."""
    start: float = Field(default=0.0, description="Start time in source media in seconds")
    duration: float = Field(..., description="Duration to use from source in seconds")

class ClipSpec(BaseModel):
    """Specification for a video clip."""
    asset_id: str = Field(..., description="Filename or unique ID of the media asset")
    source_range: Optional[SourceRange] = Field(default=None, description="Range to use from source. If None, uses full clip or fits to slot.")
    speed: float = Field(default=1.0, description="Playback speed multiplier")
    description: Optional[str] = Field(default=None, description="Description of why this clip was chosen")

class AudioClipSpec(BaseModel):
    """Specification for an audio clip."""
    asset_id: str = Field(..., description="Filename or unique ID of the audio asset")
    source_range: Optional[SourceRange] = Field(default=None, description="Range to use from source")
    volume: float = Field(default=1.0, description="Volume level (1.0 is unity)")
    fade_in: float = Field(default=0.0, description="Fade in duration in seconds")
    fade_out: float = Field(default=0.0, description="Fade out duration in seconds")

class TransitionSpec(BaseModel):
    """Specification for a transition."""
    type: Literal["cross_dissolve", "fade_to_color", "cut"] = "cross_dissolve"
    duration: float = Field(default=0.5, description="Duration of transition in seconds")
    alignment: Literal["center", "start", "end"] = "center"

class ShotInstruction(BaseModel):
    """Directive provided by the planner describing how to visualize narration."""

    shot_id: str = Field(..., description="Unique identifier for the shot directive (e.g., B011-S01)")
    narration_ids: List[str] = Field(..., description="Narration segment IDs covered by this shot")
    vo_excerpt: str = Field(..., description="Relevant excerpt of the narration text")
    visual_goal: str = Field(..., description="Description of what the audience should see")
    keywords: List[str] = Field(default_factory=list, description="Search keywords for footage")
    required_events: List[str] = Field(default_factory=list, description="Specific gameplay events or actions needed")
    preferred_assets: List[str] = Field(default_factory=list, description="Preferred footage IDs if known")
    min_clips: int = Field(default=1, description="Minimum clips needed to satisfy this shot")
    max_clips: int = Field(default=1, description="Maximum clips to place for this shot")
    coverage_note: str = Field(..., description="Reason this shot supports the narration")


class ShotPlan(BaseModel):
    """Planner output describing how to cover a beat visually."""

    beat_id: str = Field(..., description="Beat identifier this plan belongs to")
    shot_instructions: List[ShotInstruction] = Field(..., description="Ordered list of shot directives")


class DirectorNoteEntry(BaseModel):
    """Single clip placement note for review/QA."""

    beat_id: str = Field(..., description="Beat identifier")
    shot_id: str = Field(..., description="Shot directive identifier")
    narration_ids: List[str] = Field(..., description="Narration segments supported by this clip")
    vo_excerpt: str = Field(..., description="Narration text excerpt")
    vo_range: TimeSpan = Field(..., description="Timeline range of the referenced narration")
    clip_asset_id: str = Field(..., description="Media asset used")
    clip_range: SourceRange = Field(..., description="In/out points used from the asset")
    visual_summary: str = Field(..., description="What the clip shows")
    coverage_note: str = Field(..., description="Why the clip reinforces the narration")
    reasoning: str = Field(..., description="LLM or planner rationale for this pick")


class DirectorNotesDocument(BaseModel):
    """High-level document mirroring the timeline for human/LLM review."""

    project_name: str
    timeline_name: str
    entries: List[DirectorNoteEntry] = Field(default_factory=list)


class EditSegment(BaseModel):
    """
    A semantic segment of the edit. 
    Usually corresponds to a sentence or thought in the narration.
    """

    id: str = Field(..., description="Unique identifier for this segment")
    shot_id: Optional[str] = Field(default=None, description="Optional shot directive identifier this segment fulfills")

    # Timing is driven by the narration (or primary audio)
    narration_span: Optional[TimeSpan] = Field(default=None, description="Time range in the master timeline for the narration/VO")
    
    # Content
    narration_audio: Optional[AudioClipSpec] = Field(default=None, description="The VO clip for this segment")
    primary_clip: Optional[ClipSpec] = Field(default=None, description="Legacy single-clip slot (deprecated)")
    video_clips: List[ClipSpec] = Field(default_factory=list, description="Ordered list of visuals for this segment")
    broll: List[ClipSpec] = Field(default_factory=list, description="Overlay clips (B-Roll) to show during this segment")
    
    # Transitions
    transition_in: Optional[TransitionSpec] = Field(default=None, description="Transition into this segment")
    transition_out: Optional[TransitionSpec] = Field(default=None, description="Transition out of this segment")

class SemanticEdit(BaseModel):
    """The full plan for a video edit."""
    project_name: str = Field(..., description="Name of the project/timeline")
    fps: float = Field(default=24.0, description="Frame rate of the timeline")
    segments: List[EditSegment] = Field(..., description="Ordered list of edit segments")
    background_music: List[AudioClipSpec] = Field(default_factory=list, description="Background music tracks")

class Beat(BaseModel):
    """
    A logical grouping of narration segments that share a visual idea.
    This is the "Director's Plan" unit.
    """
    id: str = Field(..., description="Unique identifier for this beat (e.g., 'B001')")
    segment_ids: List[str] = Field(..., description="List of alignment IDs (e.g., ['L001', 'L002']) included in this beat")
    visual_intent: str = Field(..., description="High-level description of the visual goal")
    pacing: Literal["fast", "slow", "normal", "dynamic"] = Field(default="normal", description="Intended editing pace")
    search_queries: List[str] = Field(default_factory=list, description="Keywords to find relevant footage")

class BeatMap(BaseModel):
    """The complete collection of beats for a project."""
    beats: List[Beat]
