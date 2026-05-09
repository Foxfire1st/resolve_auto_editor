"""Download up-to-date Honkai: Star Rail resources (abilities, enemies, relics)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from urllib.request import urlopen

from gateway.profiles import ensure_profile_dirs

STAR_RAIL_RES_BASE = "https://raw.githubusercontent.com/Mar-7th/StarRailRes/master/index_new/en"
DEFAULT_FILES = {
    "character_skills.json": STAR_RAIL_RES_BASE + "/character_skills.json",
    "characters.json": STAR_RAIL_RES_BASE + "/characters.json",
    "light_cones.json": STAR_RAIL_RES_BASE + "/light_cones.json",
    "light_cone_ranks.json": STAR_RAIL_RES_BASE + "/light_cone_ranks.json",
    "light_cone_promotions.json": STAR_RAIL_RES_BASE + "/light_cone_promotions.json",
    "relics.json": STAR_RAIL_RES_BASE + "/relics.json",
    "relic_sets.json": STAR_RAIL_RES_BASE + "/relic_sets.json",
    "relic_main_affixes.json": STAR_RAIL_RES_BASE + "/relic_main_affixes.json",
    "relic_sub_affixes.json": STAR_RAIL_RES_BASE + "/relic_sub_affixes.json",
    "items.json": STAR_RAIL_RES_BASE + "/items.json",
    "character_skill_trees.json": STAR_RAIL_RES_BASE + "/character_skill_trees.json",
}


def download(url: str) -> str:
    with urlopen(url) as response:  # nosec: B310 - trusted GitHub host
        if response.status != 200:
            raise RuntimeError(f"Failed to download {url} (status {response.status})")
        data = response.read()
        return data.decode("utf-8")


def update_resource(name: str, url: str, target: Path) -> None:
    print(f"[update] Fetching {name} from {url}")
    text = download(url)
    # Validate JSON before overwriting local file.
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Downloaded {name} was not valid JSON: {exc}") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)
    tmp_path.replace(target)
    print(f"[update] Wrote {target} ({len(text):,} bytes)")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Refresh StarRail resources.")
    parser.add_argument(
        "--include-monsters",
        action="store_true",
        help="Fetch monster roster/details via hakushin API (slower).",
    )
    args = parser.parse_args(argv)

    profile_paths = ensure_profile_dirs("hsr", directories=("resources",))
    base_path = profile_paths.resources
    for filename, url in DEFAULT_FILES.items():
        update_resource(filename, url, base_path / filename)

    if args.include_monsters:
        asyncio.run(update_monsters(base_path))


async def update_monsters(base_path: Path) -> None:
    try:
        from hakushin.clients.hsr import HSRClient
        from loguru import logger as hakushin_logger
    except ImportError as exc:
        raise RuntimeError(
            "hakushin-py is required for --include-monsters. "
            "Install via `pip install git+https://github.com/seriaati/hakushin-py.git`."
        ) from exc

    try:
        hakushin_logger.disable("hakushin")
    except Exception:
        pass

    monsters_path = base_path / "monsters.json"
    monster_details_path = base_path / "monster_details.json"

    async with HSRClient() as client:
        print("[update] Fetching monster list via hakushin ...")
        monsters = await client.fetch_monsters()
        monster_list = [monster.model_dump() for monster in monsters]

        semaphore = asyncio.Semaphore(10)
        detail_map: dict[str, dict] = {}

        async def fetch_detail(monster_id: int) -> None:
            async with semaphore:
                detail = await client.fetch_monsters_detail(monster_id)
                detail_map[str(monster_id)] = detail.model_dump()

        await asyncio.gather(*(fetch_detail(monster["id"]) for monster in monster_list))

    _write_json(monsters_path, monster_list)
    _write_json(monster_details_path, detail_map)
    print(f"[update] Monster data written to {monsters_path} and {monster_details_path}")


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, ensure_ascii=False)
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[update] ERROR: {exc}", file=sys.stderr)
        raise
