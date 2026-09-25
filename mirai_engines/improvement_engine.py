from __future__ import annotations

from collections import Counter
from typing import Any

from storage import analytics_history, get_channel_state
from .debug_engine import MiraiDebugEngine
from .quality_engine import MiraiQualityEngine
from .visual_learning import VisualLearningMemory


class MiraiImprovementEngine:
    """
    Ollamaがなくても動く、ミライ自身の決定論的な改善エンジン。
    AI提案はこの結果を補足するだけで、基礎改善はここで決める。
    """

    def review(self) -> dict[str, Any]:
        quality = MiraiQualityEngine().recent(20)
        debug = MiraiDebugEngine().recent(20)
        analytics = analytics_history(20)
        visual = VisualLearningMemory().dashboard_state()

        recommendations: list[dict[str, str]] = []
        actions: list[dict[str, Any]] = []

        failed_quality = [row for row in quality if not row.get("passed")]
        issue_codes = Counter(
            issue.get("code")
            for row in failed_quality
            for issue in (row.get("issues") or [])
            if issue.get("code")
        )
        debug_categories = Counter(
            row.get("category")
            for row in debug
            if row.get("category")
        )

        if issue_codes.get("subtitle_too_long", 0) >= 2:
            recommendations.append(
                {
                    "area": "video",
                    "priority": "high",
                    "action": "字幕を短く分割する",
                    "reason": "長い字幕が複数動画で検出されています。",
                }
            )
            actions.append(
                {
                    "action_type": "script_guidance",
                    "title": "字幕を短くする",
                    "value": "1画面の字幕を短くし、意味の切れ目で細かく区切る。",
                    "reason": "Quality Engineで長字幕が繰り返し検出されたため。",
                }
            )

        gpu_failures = (
            debug_categories.get("gpu_contention", 0)
            + debug_categories.get("gpu_memory", 0)
        )
        if gpu_failures >= 2:
            recommendations.append(
                {
                    "area": "resource",
                    "priority": "high",
                    "action": "GPU負荷を自動で下げる",
                    "reason": f"GPU系診断が{gpu_failures}件あります。",
                }
            )
            actions.append(
                {
                    "action_type": "reduce_scene_images",
                    "title": "シーン画像数を1へ削減",
                    "value": 1,
                    "reason": "GPU失敗の再発防止。",
                }
            )

        if debug_categories.get("voice_service", 0) >= 2:
            recommendations.append(
                {
                    "area": "voice",
                    "priority": "medium",
                    "action": "音声provider起動確認を生成前に強化する",
                    "reason": "音声サービス接続失敗が繰り返されています。",
                }
            )

        if quality:
            avg_score = sum(int(row.get("score") or 0) for row in quality) / len(quality)
            if avg_score < 80:
                recommendations.append(
                    {
                        "area": "video",
                        "priority": "medium",
                        "action": "Quality Engineの警告を優先して動画構成を調整する",
                        "reason": f"最近の品質平均は{avg_score:.1f}点です。",
                    }
                )

        visual_avg = visual.get("avg_score")
        if visual_avg is not None and float(visual_avg) < 72:
            recommendations.append(
                {
                    "area": "image",
                    "priority": "high",
                    "action": "Visual Evolutionの候補比較と再生成を継続する",
                    "reason": (
                        "最近採用した生成素材の平均品質が"
                        f"{float(visual_avg):.1f}点です。"
                    ),
                }
            )
            actions.append(
                {
                    "action_type": "prompt_tuning",
                    "title": "Visual prompt品質補正を継続",
                    "value": {
                        "preferred_profile": visual.get("preferred_profile"),
                        "avg_score": visual_avg,
                    },
                    "reason": "Visual Quality平均が目標未満のため。",
                }
            )

        retention_values = [
            float(row.get("avg_view_percentage") or 0)
            for row in analytics
            if float(row.get("avg_view_percentage") or 0) > 0
        ]
        if len(retention_values) >= 4:
            recent = retention_values[: max(2, len(retention_values) // 3)]
            baseline = retention_values[max(2, len(retention_values) // 3) :]
            if baseline:
                recent_avg = sum(recent) / len(recent)
                base_avg = sum(baseline) / len(baseline)
                if recent_avg < base_avg * 0.9:
                    actions.append(
                        {
                            "action_type": "script_guidance",
                            "title": "冒頭テンポ改善",
                            "value": "冒頭で結論または驚きを先に出し、前置きを短くする。",
                            "reason": "直近の視聴維持率が過去平均との差で低下しています。",
                        }
                    )

        if not recommendations and not actions:
            summary = "明確な再発パターンはまだありません。現設定を維持してデータを蓄積します。"
        else:
            summary = (
                f"Quality {len(quality)}件、Debug {len(debug)}件、"
                f"Analytics {len(analytics)}件から自動改善候補を生成しました。"
            )

        return {
            "summary": summary,
            "recommendations": recommendations,
            "actions": actions,
            "requires_code_change": False,
            "source": "mirai-deterministic-engine",
            "current_guidance": {
                "script": get_channel_state("autonomous_script_guidance", ""),
                "planner": get_channel_state("autonomous_planner_guidance", ""),
                "visual": visual,
            },
        }
