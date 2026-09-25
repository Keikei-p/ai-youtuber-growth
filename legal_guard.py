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


_ZUNKO_VOICE_CREDITS = (
    "VOICEVOX:ずんだもん",
    "VOICEVOX:東北ずん子",
    "VOICEVOX:東北きりたん",
    "VOICEVOX:東北イタコ",
    "VOICEVOX:四国めたん",
    "VOICEVOX:九州そら",
    "VOICEVOX:中国うさぎ",
    "VOICEVOX:中部つるぎ",
    "VOICEVOX:あんこもん",
)


def assess_voice_license_risk(
    *,
    text: str,
    required_credit: str,
) -> list[PublishRisk]:
    """
    現在使っている音声ライブラリ固有の禁止用途。
    法律一般ではなく、音源ライセンス遵守のためのハード停止。
    """
    credit = str(required_credit or "").strip()
    if not credit or not credit.startswith(_ZUNKO_VOICE_CREDITS):
        return []

    risks: list[PublishRisk] = []

    if re.search(
        r"(政治|政党|選挙|候補者|国会|内閣|首相|大統領|政府|"
        r"政治家|政治団体|宗教|宗派|教団|宗教家|信仰|布教)",
        text,
        flags=re.IGNORECASE,
    ):
        risks.append(
            PublishRisk(
                "voice_license_politics_religion",
                "block",
                "現在のVOICEVOX音源規約では政治・宗教に関する利用を避ける必要があります。",
            )
        )

    if re.search(
        r"(情報商材|高額情報教材|高額塾|稼ぐ教材|副業教材を販売|"
        r"副業ノウハウを販売|情報教材を販売)",
        text,
        flags=re.IGNORECASE,
    ):
        risks.append(
            PublishRisk(
                "voice_license_information_product",
                "block",
                "現在のVOICEVOX音源規約では情報商材での利用・宣伝が禁止されています。",
            )
        )

    if re.search(
        r"(虚偽情報を流す|嘘の情報を流す|フェイクニュースを作る|"
        r"誤解させるために|デマを拡散)",
        text,
        flags=re.IGNORECASE,
    ):
        risks.append(
            PublishRisk(
                "voice_license_fake_content",
                "block",
                "現在のVOICEVOX音源規約では意図的な虚偽・誤解を招く内容の作成や拡散が禁止されています。",
            )
        )

    if re.search(
        r"(性風俗|風俗店|アダルトサービス|成人向けサービス|"
        r"接待飲食店の宣伝)",
        text,
        flags=re.IGNORECASE,
    ):
        risks.append(
            PublishRisk(
                "voice_license_adult_business",
                "block",
                "現在のVOICEVOX音源規約で禁止される業態に関する利用の可能性があります。",
            )
        )

    if (
        any(marker.lower() in text.lower() for marker in _REAL_ENTITY_MARKERS)
        and re.search(
            r"(応援|支持|支援|推薦|批判|非難|叩く|詐欺|犯罪|違法|"
            r"悪質|ブラック|嘘つき|騙して)",
            text,
            flags=re.IGNORECASE,
        )
    ):
        risks.append(
            PublishRisk(
                "voice_license_targeted_support_or_criticism",
                "block",
                "現在のVOICEVOX音源規約では特定の個人・団体を応援、批判、非難する目的での利用が禁止されています。",
            )
        )

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

    full_text = " ".join((title, script, description))
    hard_risks = assess_voice_license_risk(
        text=full_text,
        required_credit=required_credit,
    )

    if required_credit == "__UNRESOLVED_REQUIRED_VOICE_CREDIT__":
        hard_risks.append(
            PublishRisk(
                "unresolved_voice_credit",
                "block",
                "必須の音声クレジットを解決できないため公開できません。",
            )
        )
    elif required_credit and required_credit not in description:
        hard_risks.append(
            PublishRisk(
                "missing_voice_credit",
                "block",
                f"必要な音声クレジット「{required_credit}」が概要欄にありません。",
            )
        )

    if hard_risks:
        return {
            "allowed": False,
            "hard_blocked": True,
            "risks": [asdict(risk) for risk in hard_risks],
            "approval_id": None,
        }

    if not risks:
        return {
            "allowed": True,
            "hard_blocked": False,
            "risks": [],
            "approval_id": None,
        }

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
