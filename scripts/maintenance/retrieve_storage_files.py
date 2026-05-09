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

print("Listing files...")
for file in client.files.list():
    print(f"Name: {file.name}, Display Name: {file.display_name}, Size: {file.size_bytes}")
