from __future__ import annotations

import time
from typing import Any

from googleapiclient.discovery import build

from youtube.auth import get_credentials


class UploadVerificationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "verification_failed",
        terminal: bool = False,
        detail: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.terminal = terminal
        self.detail = detail or {}


def _norm(value: str) -> str:
    return " ".join(str(value or "").split()).strip()


def _inspect_once(
    video_id: str,
    *,
    expected_title: str,
    expected_description: str,
    expected_privacy: str,
) -> dict[str, Any]:
    youtube = build(
        "youtube",
        "v3",
        credentials=get_credentials(interactive=False),
    )
    response = youtube.videos().list(
        part="snippet,status,processingDetails,contentDetails",
        id=video_id,
    ).execute()
    items = response.get("items") or []
    if not items:
        return {
            "exists": False,
            "verified": False,
            "video_id": video_id,
            "reason": "not_found_yet",
        }

    item = items[0]
    snippet = item.get("snippet") or {}
    status = item.get("status") or {}
    processing = item.get("processingDetails") or {}
    content = item.get("contentDetails") or {}

    processing_status = str(
        processing.get("processingStatus") or ""
    ).strip().lower()
    upload_status = str(
        status.get("uploadStatus") or ""
    ).strip().lower()
    privacy = str(
        status.get("privacyStatus") or ""
    ).strip().lower()

    title_match = (
        _norm(snippet.get("title"))
        == _norm(expected_title)
    )
    description_match = (
        _norm(snippet.get("description"))
        == _norm(expected_description)
    )
    privacy_match = (
        not expected_privacy
        or privacy == str(expected_privacy).strip().lower()
    )

    terminal_failure = (
        processing_status in {"failed"}
        or upload_status in {"failed", "rejected", "deleted"}
    )
    processing_ready = (
        processing_status == "succeeded"
        and upload_status == "processed"
    )
    duration_present = bool(str(content.get("duration") or "").strip())
    thumbnails = snippet.get("thumbnails") or {}
    thumbnail_present = bool(thumbnails)

    verified = (
        processing_ready
        and duration_present
        and title_match
        and description_match
        and privacy_match
        and thumbnail_present
    )

    return {
        "exists": True,
        "verified": verified,
        "video_id": video_id,
        "processing_status": processing_status,
        "upload_status": upload_status,
        "privacy_status": privacy,
        "title": str(snippet.get("title") or ""),
        "description": str(snippet.get("description") or ""),
        "title_match": title_match,
        "description_match": description_match,
        "privacy_match": privacy_match,
        "duration": str(content.get("duration") or ""),
        "duration_present": duration_present,
        "thumbnail_present": thumbnail_present,
        "terminal_failure": terminal_failure,
        "failure_reason": str(status.get("failureReason") or ""),
        "rejection_reason": str(status.get("rejectionReason") or ""),
        "processing_failure_reason": str(
            processing.get("processingFailureReason") or ""
        ),
        # API上で処理成功+contentDetails取得済みを再生準備完了として扱う。
        # 実ブラウザの再生操作そのものではない。
        "playback_ready": processing_ready and duration_present,
    }


def verify_uploaded_video(
    video_id: str,
    *,
    expected_title: str,
    expected_description: str,
    expected_privacy: str,
    max_attempts: int = 18,
    poll_seconds: float = 5.0,
) -> dict[str, Any]:
    """
    YouTube側で動画の存在・処理完了・メタデータ・公開状態を確認する。
    videos.insertの成功だけでは完了扱いにしない。
    """
    last: dict[str, Any] = {}
    attempts = max(1, int(max_attempts))
    for attempt in range(1, attempts + 1):
        last = _inspect_once(
            video_id,
            expected_title=expected_title,
            expected_description=expected_description,
            expected_privacy=expected_privacy,
        )
        last["attempt"] = attempt

        if last.get("terminal_failure"):
            reasons = [
                last.get("failure_reason"),
                last.get("rejection_reason"),
                last.get("processing_failure_reason"),
            ]
            reason = " / ".join(
                str(value)
                for value in reasons
                if str(value or "").strip()
            ) or "YouTube processing failed"
            raise UploadVerificationError(
                f"YouTube投稿後処理に失敗しました: {reason}",
                code="youtube_processing_failed",
                terminal=True,
                detail=last,
            )

        if last.get("exists") and (
            not last.get("title_match")
            or not last.get("description_match")
            or not last.get("privacy_match")
        ):
            raise UploadVerificationError(
                "YouTube上の投稿情報が期待値と一致しません。",
                code="youtube_metadata_mismatch",
                terminal=False,
                detail=last,
            )

        if last.get("verified"):
            return last

        if attempt < attempts and poll_seconds > 0:
            time.sleep(float(poll_seconds))

    raise UploadVerificationError(
        "YouTube側の動画処理完了を確認できませんでした。"
        " 次回サイクルで同じvideoIdを再確認します。",
        code="youtube_processing_pending",
        terminal=False,
        detail=last,
    )
