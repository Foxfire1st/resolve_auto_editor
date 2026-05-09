import base64
import certifi
import json
import os
import sys
import cv2
import pandas as pd
import httpx  # using httpx instead of google.generativeai
from sklearn.feature_extraction.text import TfidfVectorizer
import subprocess
import re
import shutil
import xml.etree.ElementTree as ET
from PySide6.QtWidgets import QFileDialog
from registration import is_license_valid

MODEL_NAME = "gemini-2.0-flash"
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent"


def get_app_data_folder():
    if sys.platform == "win32":
        return os.path.join(os.getenv("APPDATA", os.path.expanduser("~\\AppData\\Roaming")), "VideoTagger")
    elif sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/VideoTagger")
    else:
        return os.path.expanduser("~/.config/VideoTagger")

def get_tool_path(tool_name):
    if sys.platform == "win32" and not tool_name.endswith(".exe"):
        tool_name += ".exe"
    base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, "tools", tool_name)

ffmpeg_path = get_tool_path("ffmpeg")
ffprobe_path = get_tool_path("ffprobe")

def get_settings_path():
    base_path = get_app_data_folder()
    os.makedirs(base_path, exist_ok=True)
    return os.path.join(base_path, "settings.json")

def load_api_key():
    google_api_key = None
    settings_path = get_settings_path()

    if os.path.exists(settings_path):
        try:
            with open(settings_path, "r") as file:
                config = json.load(file)
                google_api_key = config.get("api_key", "").strip()
                if google_api_key:
                    print("✅ Google Gemini API Key Loaded")
                else:
                    print("❌ Google Gemini API Key is missing.")
        except Exception as e:
            print(f"❌ ERROR loading settings: {e}")
    else:
        print("❌ ERROR: settings.json file not found.")
    
    return google_api_key

def get_keyframes_folder():
    import os
    folder = os.path.join(os.path.expanduser("~"), "Library/Application Support/VideoTagger/keyframes")
    os.makedirs(folder, exist_ok=True)
    return folder

def extract_keyframes(video_path):
    """Extracts one keyframe at the midpoint of the video."""
    output_dir = get_keyframes_folder()
    os.makedirs(output_dir, exist_ok=True)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise Exception(f"Cannot open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total_frames / fps if fps else 0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    frame_index = total_frames // 2
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
    ret, frame = cap.read()

    keyframe_paths = []
    if ret:
        keyframe_path = os.path.join(output_dir, f"{os.path.basename(video_path)}_keyframe_mid.jpg")
        cv2.imwrite(keyframe_path, frame)
        keyframe_paths.append(keyframe_path)

    cap.release()
    resolution = f"{width}x{height}"
    return keyframe_paths, duration, fps, resolution

def describe_keyframes_with_gemini_flash(keyframe_paths, custom_prompt=None, output_display=None):
    api_key = load_api_key()
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key
    }
    descriptions = []
    prompt = custom_prompt or '''
    Analyze the image and deliver a strong description of what you see. From that description generate also a filename and tags.

    For filenames use identified names or descriptors.
    Scenary = The type of screen you identified like "Combat", "Party Setup", "Idle" (e.g. Character posing in front of a landscape), "Weapons", "Artifacts", "Constellations", "Talents", etc.
    Subjects = Can be Character names or item names.
    Apply this format to the filename and replace the placeholders with what you identified. => [Scenary]_[Subject1]_[Subject2]_[Subject3]...
    '''

    for keyframe_path in keyframe_paths:
        try:
            msg = f"🔹 Sending {keyframe_path} to Gemini API"
            if output_display:
                output_display.append(msg)
            else:
                print(msg)

            with open(keyframe_path, "rb") as image_file:
                image_bytes = image_file.read()

            image_base64 = base64.b64encode(image_bytes).decode("utf-8")

            payload = {
                "contents": [
                    {"parts": [
                        {"text": prompt},
                        {"inlineData": {"mimeType": "image/jpeg", "data": image_base64}}
                    ]}
                ]
            }

            response = httpx.post(API_URL, headers=headers, json=payload, verify=certifi.where())
            if response.status_code == 200:
                result = response.json()
                description = result["candidates"][0]["content"]["parts"][0]["text"].strip()
                descriptions.append(description)
                if output_display:
                    output_display.append(f"✅ AI Description: {description}")
                else:
                    print(f"✅ AI Description: {description}")
            else:
                descriptions.append("Error")
                msg = f"❌ API Error {response.status_code}: {response.text}"
                if output_display:
                    output_display.append(msg)
                else:
                    print(msg)

        except Exception as e:
            descriptions.append("Error")
            msg = f"❌ AI Error while processing {keyframe_path}: {e}"
            if output_display:
                output_display.append(msg)
            else:
                print(msg)

    return descriptions

# Generate a master description
def generate_master_description(descriptions):
    match = re.search(r"\*\*Description:\*\*\s*(.*?)\s*\*\*Filename"," ".join(descriptions), re.DOTALL)
    if match:
        return match.group(1).strip()
    return None

def extract_keywords_from_description(description, top_n=10):
    """Extract top N keywords from the AI-generated description using TF-IDF."""
    vectorizer = TfidfVectorizer(stop_words='english')
    X = vectorizer.fit_transform([description])
    feature_names = vectorizer.get_feature_names_out()
    tfidf_scores = X.toarray()[0]
    sorted_indices = tfidf_scores.argsort()[-top_n:][::-1]
    keywords = [feature_names[i] for i in sorted_indices]
    return keywords

def merge_with_important_keywords(ai_keywords, user_keywords_str):
    """Merge AI-generated keywords with user-provided ones (comma-separated)."""
    user_keywords = [kw.strip() for kw in user_keywords_str.split(",") if kw.strip()]
    combined_keywords = list(set(ai_keywords + user_keywords))  # Remove duplicates
    return combined_keywords

# Extract keywords for filename generation
def extract_keywords(description):
    match = re.search(r"\*\*Filename:\*\*\s*(.*?)\s*\*\*Tags", description, re.DOTALL)
    if match:
        return match.group(1).strip()
    return None

def process_videos(video_paths, selected_options, apply_metadata_button, output_display=None):
    if not is_license_valid():
        msg = "⚠️ License key missing or invalid. Please activate the software."
        if output_display:
            output_display.append(msg)
        else:
            print(msg)
        return {}

    api_key = load_api_key()
    if not api_key:
        msg = "❌ ERROR: No API key provided! AI processing will fail."
        if output_display:
            output_display.append(msg)
        else:
            print(msg)
        return {}

    msg = "🔹 Using configured API key"
    if output_display:
        output_display.append(msg)
    else:
        print(msg)

    custom_prompt = selected_options.get("custom_prompt", "")
    important_keywords = selected_options.get("important_keywords", "")
    creator = selected_options.get("creator", "")
    copyright_text = selected_options.get("copyright", "")

    video_metadata = {}

    for video_path in video_paths:
        msg = f"🔹 Processing video: {video_path}"
        if output_display:
            output_display.append(msg)
        else:
            print(msg)

        try:
            keyframe_paths, duration, fps, resolution = extract_keyframes(video_path)
        except Exception as e:
            msg = f"❌ Error extracting keyframes from {video_path}: {e}"
            if output_display:
                output_display.append(msg)
            else:
                print(msg)
            continue

        if not keyframe_paths:
            msg = f"⚠️ No keyframes extracted for {video_path}"
            if output_display:
                output_display.append(msg)
            else:
                print(msg)
            continue

        descriptions = describe_keyframes_with_gemini_flash(keyframe_paths, custom_prompt=custom_prompt, output_display=output_display)
        master_description = generate_master_description(descriptions)

        if "error" in master_description.lower() or not master_description.strip():
            msg = f"❌ AI failed to generate a valid description for {video_path}"
            if output_display:
                output_display.append(msg)
            else:
                print(msg)
            continue

        ai_keywords = extract_keywords_from_description(master_description)
        final_keywords = merge_with_important_keywords(ai_keywords, important_keywords)

        shortened_filename = extract_keywords(master_description) + ".mp4"

        video_metadata[video_path] = {
            "master_description": master_description,
            "shortened_filename": shortened_filename,
            "duration": duration,
            "fps": fps,
            "resolution": resolution,
            "keywords": final_keywords,
            "creator": creator,
            "copyright": copyright_text
        }

    if not video_metadata:
        msg = "❌ No videos were successfully processed. Please check for API errors or keyframe extraction issues."
        if output_display:
            output_display.append(msg)
        else:
            print(msg)

    msg = f"🔹 Final metadata before saving: {video_metadata}"
    if output_display:
        output_display.append(msg)
    else:
        print(msg)

    save_metadata_to_csv(video_metadata)

    if apply_metadata_button:
        apply_metadata_button.setEnabled(True)

    return video_metadata

# Save metadata to CSV
import os
import pandas as pd

def get_metadata_path():
    """Returns the absolute path for metadata.csv."""
    base_path = get_app_data_folder()
    os.makedirs(base_path, exist_ok=True)  # Ensure the folder exists
    return os.path.join(base_path, "metadata.csv")

def ensure_metadata_file():
    """Creates metadata.csv if it does not exist."""
    metadata_path = get_metadata_path()
    if not os.path.exists(metadata_path):
        print("⚠️ metadata.csv not found. Creating a new empty file.")
        df = pd.DataFrame(columns=["video_filename", "duration", "fps", "resolution", "description", "shortened_filename"])
        df.to_csv(metadata_path, index=False)
    return metadata_path

# ✅ Call this function at startup
def save_metadata_to_csv(video_metadata):
    metadata_path = get_metadata_path()

    print(f"🔹 Saving metadata to: {metadata_path}")
    print(f"🔹 Video metadata received: {video_metadata}")

    if not video_metadata:
        print("❌ No metadata received, skipping save.")
        return

    try:
        data = [
            {
                "video_filename": video_path,
                "duration": metadata["duration"],
                "fps": metadata["fps"],
                "resolution": metadata["resolution"],
                "description": metadata["master_description"],
                "shortened_filename": metadata["shortened_filename"]
            }
            for video_path, metadata in video_metadata.items()
        ]

        if not data:
            print("⚠️ No valid data found, skipping write operation.")
            return

        df = pd.DataFrame(data)
        df.to_csv(metadata_path, index=False)
        print("✅ Metadata saved successfully!")

    except Exception as e:
        print(f"❌ ERROR saving metadata: {e}")

def load_video_metadata():
    """Reads metadata.csv and structures it for exporting."""
    metadata_path = get_metadata_path()
    
    if not os.path.exists(metadata_path):
        return {}

    metadata_df = pd.read_csv(metadata_path)
    video_metadata = {}

    for _, row in metadata_df.iterrows():
        video_metadata[row["video_filename"]] = {
            "shortened_filename": row["shortened_filename"],
            "master_description": row["description"],
            "duration": row["duration"],
            "fps": row["fps"],
            "resolution": row["resolution"]
        }
    
    return video_metadata

def apply_metadata_to_videos(csv_file="metadata.csv", output_folder="output"):
    """Apply metadata to videos and save them in a custom folder."""
    import pandas as pd
    import os

    os.makedirs(output_folder, exist_ok=True)  # Ensure the folder exists

    metadata_df = pd.read_csv(csv_file)

    for index, row in metadata_df.iterrows():
        original_video_file = row['video_filename']
        shortened_filename = row['shortened_filename']
        description = row['description']

        # Define new output path using the selected folder
        output_video_path = os.path.join(output_folder, shortened_filename)

        # Ensure file exists before processing
        if not os.path.exists(original_video_file):
            print(f"❌ File not found: {original_video_file}. Skipping.")
            continue

        # ✅ FFmpeg Rewrap (Preserves metadata, enables fast start)
        ffmpeg_command = [
            ffmpeg_path,
            "-hide_banner", "-loglevel", "error",   # Reduce output noise
            "-i", original_video_file,
            "-movflags", "+faststart",
            "-c", "copy",
            output_video_path
        ]

        print(f"▶️ Running FFmpeg:\n{' '.join(ffmpeg_command)}")

        result = subprocess.run(ffmpeg_command, capture_output=True, text=True)

        if result.returncode != 0:
            print(f"❌ FFmpeg Error:\n{result.stderr}")
        else:
            print(f"✅ Video rewrapped and saved: {output_video_path}")

        # Apply metadata using ExifTool
        #exiftool_path = shutil.which("exiftool") or "/opt/homebrew/bin/exiftool"  # Adjust based on your system

        #exiftool_command = [
            #exiftool_path,
            #f"-QuickTime:Title={shortened_filename}",
            #"-QuickTime:Artist=AI Video Tagger",
            #f"-QuickTime:Description={description}",
            #"-overwrite_original",
            #output_video_path
        #]
        #subprocess.run(exiftool_command, capture_output=True, text=True, check=False)

        print(f"✅ Metadata applied to {output_video_path}")

# Export CSV for editors
def export_csv(video_metadata, filename, columns, delimiter=","):
    if not video_metadata:
        video_metadata = load_video_metadata()

    data = [
        {col: metadata.get(col, "") for col in columns}
        for metadata in video_metadata.values()
    ]
    pd.DataFrame(data).to_csv(filename, index=False, sep=delimiter)
    print(f"✅ Exported: {filename}")

def export_csv_davinci(video_metadata):
    """Exports metadata in CSV format for DaVinci Resolve."""
    filename, _ = QFileDialog.getSaveFileName(None, "Save CSV", "metadata_davinci.csv", "CSV Files (*.csv)")
    if not filename:
        return

    data = []
    for video_path, metadata in video_metadata.items():
        row = {
            "File Name": metadata.get("shortened_filename", os.path.basename(video_path)),
            "Description": metadata.get("master_description", ""),
            "Keywords": ", ".join(metadata.get("keywords", [])) if "keywords" in metadata else "",
            "Scene": "",  # DaVinci allows empty Scene
            "Shot": "",   # DaVinci allows empty Shot
            "Notes": ""
        }
        data.append(row)

    df = pd.DataFrame(data)
    df.to_csv(filename, index=False)
    print(f"✅ DaVinci Resolve CSV exported: {filename}")

def export_csv_premiere(video_metadata):
    """Exports metadata in CSV format for Adobe Premiere Pro."""
    filename, _ = QFileDialog.getSaveFileName(None, "Save CSV", "metadata_premiere.csv", "CSV Files (*.csv)")
    if not filename:
        return

    data = []
    for video_path, metadata in video_metadata.items():
        row = {
            "Clip Name": metadata.get("shortened_filename", os.path.basename(video_path)),
            "Description": metadata.get("master_description", ""),
            "Tags": ", ".join(metadata.get("keywords", [])) if "keywords" in metadata else "",
            "Scene": "",
            "Shot": "",
            "Notes": ""
        }
        data.append(row)

    df = pd.DataFrame(data)
    df.to_csv(filename, index=False)
    print(f"✅ Premiere Pro CSV exported: {filename}")

def export_csv_avid(video_metadata):
    """Exports metadata in TAB-delimited format for Avid Media Composer."""
    filename, _ = QFileDialog.getSaveFileName(None, "Save Avid CSV", "metadata_avid.txt", "Text Files (*.txt)")
    if not filename:
        return

    data = []
    for video_path, metadata in video_metadata.items():
        row = [
            metadata.get("shortened_filename", os.path.basename(video_path)),
            metadata.get("master_description", ""),
            ", ".join(metadata.get("keywords", [])) if "keywords" in metadata else "",
            "",  # Scene
            "",  # Shot
            ""  #Notes
        ]
        data.append(row)

    df = pd.DataFrame(data, columns=["File Name", "Description", "Keywords", "Scene", "Shot", "Notes"])
    df.to_csv(filename, index=False, sep="\t")  # TAB-separated
    print(f"✅ Avid Media Composer CSV exported: {filename}")

import xml.etree.ElementTree as ET

def export_xml_fcp(video_metadata):
    """Exports metadata in XML format for Final Cut Pro."""
    filename, _ = QFileDialog.getSaveFileName(None, "Save FCPX XML", "metadata_fcpx.xml", "XML Files (*.xml)")
    if not filename:
        return

    root = ET.Element("fcpxml", version="1.10")
    resources = ET.SubElement(root, "resources")

    for video_path, metadata in video_metadata.items():
        clip = ET.SubElement(root, "clip", name=metadata.get("shortened_filename", os.path.basename(video_path)))
        ET.SubElement(clip, "description").text = metadata.get("master_description", "")
        ET.SubElement(clip, "keywords").text = ", ".join(metadata.get("keywords", [])) if "keywords" in metadata else ""
        ET.SubElement(clip, "notes").text = "AI-generated metadata"

    tree = ET.ElementTree(root)
    tree.write(filename, encoding="utf-8", xml_declaration=True)
    print(f"✅ Final Cut Pro XML exported: {filename}")

def export_csv_iptc(video_metadata, parent=None):
    """Export metadata to a CSV using the IPTC standard fields."""
    if not video_metadata:
        video_metadata = load_video_metadata()

    filename, _ = QFileDialog.getSaveFileName(
        parent, "Save IPTC CSV", "iptc_metadata.csv", "CSV Files (*.csv)"
    )

    if not filename:
        return

    # Standard IPTC fields we'll use
    columns = {
        "Object Name": "shortened_filename",
        "Caption/Abstract": "master_description",
        "Keywords": "keywords",
        "Writer/Editor": "creator",
        "Copyright Notice": "copyright",
        "Source": "VideoTaggerApp",
        "Headline": "shortened_filename"
    }

    data = []

    for path, metadata in video_metadata.items():
        row = {}
        for iptc_field, meta_key in columns.items():
            if meta_key == "keywords":
                row[iptc_field] = ", ".join(metadata.get("keywords", [])) if "keywords" in metadata else ""
            elif meta_key in metadata:
                row[iptc_field] = metadata[meta_key]
            else:
                row[iptc_field] = ""
        data.append(row)

    df = pd.DataFrame(data)
    df.to_csv(filename, index=False)
    print(f"✅ IPTC CSV exported to: {filename}")

import glob

def clear_keyframes_folder():
    folder = get_keyframes_folder()
    for f in glob.glob(os.path.join(folder, "*.jpg")):
        os.remove(f)

_ = extract_keyframes, get_keyframes_folder
