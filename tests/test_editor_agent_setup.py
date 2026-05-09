import unittest
from pathlib import Path
import shutil
import json
from src.orchestrator.editor_agent import EditorAgent, EditorSessionConfig, TOOL_REGISTRY
from src.orchestrator import models

class TestEditorAgentSetup(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path("c:/ew/resolve_auto_editor/tests/temp_agent_test")
        self.test_dir.mkdir(parents=True, exist_ok=True)
        
        self.beat_map_path = self.test_dir / "beat_map.json"
        self.alignment_path = self.test_dir / "alignment.json"
        self.output_dir = self.test_dir / "output"
        
        # Create dummy beat map
        beat_map = models.BeatMap(beats=[
            models.Beat(
                id="B001",
                segment_ids=["L001"],
                visual_intent="Intro",
                pacing="normal"
            )
        ])
        with open(self.beat_map_path, "w") as f:
            f.write(beat_map.model_dump_json())
            
        # Create dummy alignment
        alignment = {
            "segments": [
                {"id": "L001", "text": "Hello world", "t0": 0.0, "t1": 2.0}
            ]
        }
        with open(self.alignment_path, "w") as f:
            json.dump(alignment, f)
            
        self.config = EditorSessionConfig(
            project_name="TestProject",
            timeline_name="TestTimeline",
            script_alignment_path=self.alignment_path,
            beat_map_path=self.beat_map_path,
            output_dir=self.output_dir
        )

    def tearDown(self):
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)

    def test_agent_initialization(self):
        agent = EditorAgent(self.config)
        self.assertIsNotNone(agent.state)
        self.assertEqual(len(agent.state.beat_map.beats), 1)
        self.assertEqual(agent.state.beat_map.beats[0].id, "B001")

    def test_tool_registry(self):
        self.assertIn("get_beat_map", TOOL_REGISTRY)
        self.assertIn("choose_clip_for_shot", TOOL_REGISTRY)

if __name__ == "__main__":
    unittest.main()
