import sys
from pathlib import Path
import os
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).parent.parent
# Add repo root and src/ for absolute imports (src.*) and package-style imports.
sys.path.append(str(ROOT_DIR))
sys.path.append(str(ROOT_DIR / "src"))

from orchestrator.tool_bridge import retrieval_tools

def test_search():
    load_dotenv()
    print("Searching for 'Feixiao ultimate'...")
    results = retrieval_tools.find_clips("Feixiao ultimate", limit=3)
    for r in results:
        print(f"Found: {r.asset_id} ({r.start}-{r.end}) - Score: {r.score}")
        print(f"Desc: {r.description[:100]}...")

if __name__ == "__main__":
    test_search()
