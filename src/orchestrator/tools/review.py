import os
import time
from pathlib import Path
from typing import Dict, Any
from google import genai
from google.genai import types

def upload_video_for_review(path: str) -> str:
    """
    Uploads a video file to Gemini for processing.
    Returns the file name/ID.
    """
    api_key = (
        os.environ.get("gemini_api_key")
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
    )
    if not api_key:
        raise RuntimeError("Gemini API key not found")
        
    client = genai.Client(api_key=api_key)
    
    file_path = Path(path)
    if not file_path.exists():
        raise ValueError(f"File {path} not found")
        
    # Upload
    video_file = client.files.upload(path=file_path)
    
    # Wait for processing
    while video_file.state.name == "PROCESSING":
        time.sleep(2)
        video_file = client.files.get(name=video_file.name)
        
    if video_file.state.name == "FAILED":
        raise RuntimeError(f"Video processing failed: {video_file.state.name}")
        
    return video_file.name

def summarize_video(file_name: str, expectations: str) -> Dict[str, Any]:
    """
    Asks Gemini to watch the video and critique it against expectations.
    """
    api_key = (
        os.environ.get("gemini_api_key")
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
    )
    if not api_key:
        raise RuntimeError("Gemini API key not found")
        
    client = genai.Client(api_key=api_key)
    model_name = os.environ.get("GEMINI_MODEL_EDITOR", "gemini-2.5-flash")
    
    prompt = f"""
    Please watch this video and critique it based on the following expectations:
    
    {expectations}
    
    Provide a summary of what is shown, and list any major discrepancies or issues found.
    Return the result as a JSON object with keys: "summary", "issues", "score" (1-10).
    """
    
    # We need to pass the file object. 
    # In genai v1/v2, we can pass the file name/uri in the content.
    # Assuming file_name is the resource name (e.g. 'files/...')
    
    response = client.models.generate_content(
        model=model_name,
        contents=[
            types.Part.from_uri(file_uri=file_name, mime_type="video/mp4"),
            types.Part.from_text(text=prompt)
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json"
        )
    )
    
    try:
        import json
        return json.loads(response.text)
    except Exception as e:
        return {"error": str(e), "raw_text": response.text}
