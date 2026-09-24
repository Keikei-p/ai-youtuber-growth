from __future__ import annotations
import argparse
import json
from datetime import datetime
from pathlib import Path

from config import settings
from planner import plan_ideas
from reviewer import review_script
from storage import init_db, recent_videos, save_video, export_json
from writer import write_script

def load_character() -> dict:
    return json.loads(Path("character/character.json").read_text(encoding="utf-8"))

def run_plan_only() -> list[dict]:
    init_db()
    character = load_character()
    recent = recent_videos(30)
    ideas = plan_ideas(character, recent, settings.posts_per_day)

    results: list[dict] = []
    for idea in ideas:
        written = write_script(character, idea, recent)
        ok, issues = review_script(written["title"], written["script"], recent)

        if not ok:
            print(f"[SKIP] 品質チェックNG: {issues}")
            continue

        video_id = save_video(
            idea=idea["idea"],
            angle=idea.get("angle", ""),
            title=written["title"],
            script=written["script"],
            status="planned",
        )

        result = {
            "id": video_id,
            "idea": idea,
            "title": written["title"],
            "script": written["script"],
            "description": written.get("description", ""),
            "status": "planned",
        }
        results.append(result)
        recent.insert(0, result)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    export_json(Path(f"output/plans/{stamp}.json"), results)
    return results

def main() -> None:
    parser = argparse.ArgumentParser(description="成長型AI YouTuber v1")
    parser.add_argument("--show", action="store_true", help="生成結果を詳しく表示")
    args = parser.parse_args()

    results = run_plan_only()
    print(f"\n生成完了: {len(results)}本 / 目標 {settings.posts_per_day}本")
    for item in results:
        print(f"- #{item['id']} {item['title']}")
        if args.show:
            print(item["script"])
            print()

if __name__ == "__main__":
    main()
