from pathlib import Path

from orchestrator.models import (
    SemanticEdit,
    EditSegment,
    ClipSpec,
    AudioClipSpec,
    TimeSpan,
    SourceRange,
)
from gateway.otio_fcpxml import builder

def test_build_semantic_timeline():
    # Mock Data
    # Assuming we have some clips in the sample folder
    # sparklerun.mp4, topazrobinMOC.mp4
    
    seg1 = EditSegment(
        id="seg1",
        narration_span=TimeSpan(start=0.0, duration=5.0),
        # narration_audio=AudioClipSpec(asset_id="feixiao_vo_60s.wav", source_range=SourceRange(start=0, duration=5)),
        primary_clip=ClipSpec(asset_id="sparklerun.mp4", source_range=SourceRange(start=0, duration=5)),
    )
    
    seg2 = EditSegment(
        id="seg2",
        narration_span=TimeSpan(start=5.0, duration=5.0),
        primary_clip=ClipSpec(asset_id="topazrobinMOC.mp4", source_range=SourceRange(start=10, duration=5)),
        broll=[
            ClipSpec(asset_id="sparklerun.mp4", source_range=SourceRange(start=10, duration=2))
        ]
    )
    
    edit = SemanticEdit(
        project_name="Semantic Test",
        fps=24.0,
        segments=[seg1, seg2]
    )
    
    print("Building OTIO timeline...")
    timeline = builder.build_otio_timeline_from_model(edit)
    
    output_path = Path("samples/feixiao test edit/outputs/semantic_test.fcpxml")
    print(f"Exporting to {output_path}...")
    builder.export_fcpxml(timeline, output_path)
    
    if output_path.exists():
        print("Success! FCPXML created.")
    else:
        print("Failed to create FCPXML.")

if __name__ == "__main__":
    test_build_semantic_timeline()
