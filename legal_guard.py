from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from autonomy_policy import request_approval
from storage import get_channel_state


@dataclass(frozen=True)
class PublishRisk:
    code: str
    severity: str
    reason: str


# 自動投稿で特に避けるべき領域。
# 誤検知は「承認待ち」になるだけで、公開事故より安全側を優先する。
_RISK_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (
        r"(詐欺師|犯罪者|逮捕|容疑者|不倫|反社|暴力団|薬物|性犯罪|殺人)",
        "defamation_or_allegation",
        "実在人物・組織に関する名誉・信用侵害につながり得る断定表現",
    ),
    (
        r"(絶対に儲かる|必ず儲かる|100%儲かる|投資助言|買うべき株|FXで稼ぐ|仮想通貨で稼ぐ)",
        "financial_advice",
        "金融・投資に関する強い勧誘・断定",
    ),
    (
        r"(診断します|治療すれば治る|この薬を飲めば|医師不要|病院に行く必要はない)",
        "medical_advice",
        "医療に関する診断・治療の断定",
    ),
    (
        r"(法律相談|訴えれば勝てる|必ず勝訴|違法です|合法です)",
        "legal_advice",
        "個別の法律判断と受け取られ得る断定",
    ),
    (
        r"(歌詞をそのまま|全文引用|セリフをそのまま|漫画をそのまま|アニメ映像|映画映像)",
        "copyright_reproduction",
        "他人の著作物の実質的な複製につながり得る内容",
    ),
    (
        r"(本人の声そっくり|声を完全再現|本人になりすます|ディープフェイク)",
        "impersonation",
        "実在人物の声・容姿のなりすましにつながり得る内容",
    ),
)

_REAL_ENTITY_MARKERS = (
    "株式会社",
    "有限会社",
    "代表取締役",
    "CEO",
    "社長",
    "芸能人",
    "俳優",
    "歌手",
    "政治家",
    "選手",
)


def assess_publish_risk(
    *,
    title: str,
    script: str,
    description: str = "",
) -> list[PublishRisk]:
    text = " ".join((title or "", script or "", description or ""))
    risks: list[PublishRisk] = []

    for pattern, code, reason in _RISK_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            risks.append(PublishRisk(code, "high", reason))

    # 実在組織・人物を示しやすい語があり、同時に攻撃・断定語がある場合だけ止める。
    if any(marker.lower() in text.lower() for marker in _REAL_ENTITY_MARKERS):
        if re.search(
            r"(最悪|悪質|詐欺|違法|犯罪|危険企業|ブラック|嘘つき|騙して)",
            text,
            flags=re.IGNORECASE,
        ):
            risks.append(
                PublishRisk(
                    "entity_reputation",
                    "high",
                    "特定可能な人物・企業の信用を傷つける可能性がある表現",
                )
            )

    # 重複除去
    unique: dict[str, PublishRisk] = {}
    for risk in risks:
        unique.setdefault(risk.code, risk)
    return list(unique.values())


def _approval_key(video_id: int) -> str:
    return f"legal_publish_approved_{int(video_id)}"


def is_publish_approved(video_id: int) -> bool:
    return (
        get_channel_state(_approval_key(video_id), "false")
        .strip()
        .lower()
        == "true"
    )


def publish_gate(
    item: dict[str, Any],
    *,
    required_credit: str = "",
) -> dict[str, Any]:
    video_id = int(item.get("id") or item.get("video_id") or 0)
    title = str(item.get("title") or "")
    script = str(item.get("script") or "")
    description = str(item.get("description") or "")

    risks = assess_publish_risk(
        title=title,
        script=script,
        description=description,
    )

    if required_credit and required_credit not in description:
        risks.append(
            PublishRisk(
                "missing_voice_credit",
                "high",
                f"必要な音声クレジット「{required_credit}」が概要欄にありません。",
            )
        )

    if not risks:
        return {"allowed": True, "risks": [], "approval_id": None}

    if video_id > 0 and is_publish_approved(video_id):
        return {
            "allowed": True,
            "risks": [asdict(risk) for risk in risks],
            "approval_id": None,
            "approved_override": True,
        }

    approval_id = request_approval(
        action_type="content_risk_publish",
        title=f"動画#{video_id or '?'} 公開前確認: {title[:80]}",
        reason=" / ".join(risk.reason for risk in risks),
        payload={
            "video_id": video_id,
            "risk_codes": [risk.code for risk in risks],
            "title": title[:200],
        },
    )
    return {
        "allowed": False,
        "risks": [asdict(risk) for risk in risks],
        "approval_id": approval_id,
    }
