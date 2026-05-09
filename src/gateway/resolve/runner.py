"""Entry point for Resolve automation running via fuscript."""

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints

# Ensure src is in sys.path
# This script is at src/gateway/resolve/runner.py
# Root is 3 levels up
try:
    project_root = Path(__file__).parents[3]
except NameError:
    # Fallback if __file__ is not defined (fuscript)
    # We assume the script is run from the project root or we can derive it
    # But fuscript might be run from anywhere.
    # However, the caller (ResolveTools) passes the full path to this script.
    # But inside the script, we don't know our own path if __file__ is missing.
    # We can try to use sys.argv[0] if it points to the script.
    # In fuscript, sys.argv[0] is usually the script path.
    project_root = Path(sys.argv[0]).parents[3]

if str(project_root / "src") not in sys.path:
    sys.path.append(str(project_root / "src"))

# Try to load .env manually since fuscript lacks python-dotenv
env_path = project_root / ".env"
if env_path.exists():
    try:
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip()
                    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                        value = value[1:-1]
                    if os.name == 'nt':
                        value = os.path.expandvars(value)
                    os.environ[key] = value
    except Exception:
        pass # Best effort

from gateway.resolve.automation import ResolveAutomation

def json_serializer(obj):
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"Type {type(obj)} not serializable")

def main():
    parser = argparse.ArgumentParser(description="Run Resolve automation tasks.")
    parser.add_argument("--project", required=True, help="Resolve project name")
    parser.add_argument("--method", required=True, help="Method to call on ResolveAutomation")
    parser.add_argument("--kwargs", required=True, help="JSON string of keyword arguments")
    
    args = parser.parse_args()
    
    try:
        kwargs = json.loads(args.kwargs)
        
        # Convert path strings to Path objects where appropriate?
        # Or let the method handle it? 
        # ResolveAutomation methods expect Path objects.
        # We can inspect the method signature or just convert known keys.
        # For simplicity, let's convert any string that looks like an absolute path?
        # No, that's risky.
        # Let's convert specific keys based on the method, or just update ResolveAutomation to accept str | Path.
        # Updating ResolveAutomation is cleaner, but I just created it.
        # Let's just convert in the runner for now based on convention or just let Path(str) handle it inside the method if it was typed as Path?
        # Python doesn't auto-cast.
        
        # Let's inspect the method signature to cast types.
        import inspect
        automation = ResolveAutomation(project_name=args.project)
        method = getattr(automation, args.method)
        sig = inspect.signature(method)
        type_hints = get_type_hints(method)

        def _coerce_path(value: str | Path) -> Path:
            path = Path(value)
            if not path.is_absolute():
                path = (project_root / path).resolve()
            else:
                path = path.resolve()
            return path

        def _annotation_includes_path(annotation: Any) -> bool:
            if annotation is inspect._empty:
                return False
            if annotation is Path:
                return True
            origin = get_origin(annotation)
            if origin is None:
                return False
            return any(_annotation_includes_path(arg) for arg in get_args(annotation))

        bound_args = sig.bind_partial(**kwargs)
        for name, value in bound_args.arguments.items():
            annotation = type_hints.get(name, sig.parameters[name].annotation)
            if not _annotation_includes_path(annotation):
                continue
            if isinstance(value, Path):
                bound_args.arguments[name] = _coerce_path(value)
            elif isinstance(value, str):
                bound_args.arguments[name] = _coerce_path(value)
                
        result = method(**bound_args.arguments)
        
        print(json.dumps({"status": "success", "result": result}, default=json_serializer))
        
    except Exception as e:
        # Print error to stdout as JSON so the caller can parse it
        error_info = {
            "status": "error",
            "message": str(e),
            "traceback": traceback.format_exc()
        }
        print(json.dumps(error_info))
        sys.exit(1)

if __name__ == "__main__":
    main()
