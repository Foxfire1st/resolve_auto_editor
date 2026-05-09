import os

from dotenv import load_dotenv
from google import genai

load_dotenv()

api_key = (
    os.getenv("GOOGLE_API_KEY")
    or os.getenv("GEMINI_API_KEY")
    or os.getenv("GOOGLE_GEMINI_API_KEY")
    or os.getenv("gemini_api_key")
)
if not api_key:
    raise RuntimeError("Missing Google Gemini API key. Check your .env before running this script.")

client = genai.Client(api_key=api_key)

files = client.files.list()
print("Deleting files...")

for file in files:
    try:
        client.files.delete(name=file.name)
        print(f"Deleted: {file.display_name} ({file.name})")
    except Exception as e:
        print(f"Error deleting {file.name}: {e}")
