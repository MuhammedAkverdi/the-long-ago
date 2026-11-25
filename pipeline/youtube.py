"""YouTube Data API v3: upload (private, AI disclosure on), thumbnail, captions, scheduling."""
from __future__ import annotations

import pathlib
import time

from .config import CFG, MOCK, env, log

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.force-ssl"]
_service = None


def service():
    global _service
    if _service is None:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        creds = Credentials(None, refresh_token=env("YT_REFRESH_TOKEN"),
                            token_uri="https://oauth2.googleapis.com/token",
                            client_id=env("YT_CLIENT_ID"), client_secret=env("YT_CLIENT_SECRET"),
                            scopes=SCOPES)
        _service = build("youtube", "v3", credentials=creds, cache_discovery=False)
    return _service


def _clean(text: str, limit: int) -> str:
    return text.replace("<", "(").replace(">", ")")[:limit]


def _status(privacy: str, publish_at: str | None = None) -> dict:
    status = {"privacyStatus": privacy, "selfDeclaredMadeForKids": False,
              "containsSyntheticMedia": True,  # YouTube "altered or synthetic content" disclosure
              "embeddable": True, "license": "youtube"}
    if publish_at:
        status["publishAt"] = publish_at
    return status


def upload(path: pathlib.Path, title: str, description: str, tags: list[str]) -> str:
    if MOCK:
        return "MOCK_VIDEO_ID"
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    tags_clean, total = [], 0
    for t in tags:  # YouTube caps tags at ~500 chars total
        t = _clean(t, 60)
        if total + len(t) + 2 > 480:
            break
        tags_clean.append(t)
        total += len(t) + 2
    body = {"snippet": {"title": _clean(title, 100), "description": _clean(description, 4900),
                        "tags": tags_clean, "categoryId": CFG["channel"]["category_id"],
                        "defaultLanguage": CFG["channel"]["language"],
                        "defaultAudioLanguage": CFG["channel"]["language"]},
            "status": _status("private")}
    media = MediaFileUpload(str(path), chunksize=16 * 1024 * 1024, resumable=True, mimetype="video/mp4")
    request = service().videos().insert(part="snippet,status", body=body, media_body=media)
    response, errors = None, 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                log(f"upload {int(status.progress() * 100)}%")
        except HttpError as exc:
            if exc.resp.status in (500, 502, 503, 504) and errors < 8:
                errors += 1
                time.sleep(2 ** errors)
                continue
            raise
        except (ConnectionError, TimeoutError, OSError):
            if errors >= 8:
                raise
            errors += 1
            time.sleep(2 ** errors)
    video_id = response["id"]
    log(f"uploaded video {video_id}")
    return video_id


def set_thumbnail(video_id: str, path: pathlib.Path) -> bool:
    if MOCK:
        return True
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload
    try:
        service().thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(path), mimetype="image/jpeg")).execute()
        return True
    except HttpError as exc:  # usually: channel not phone-verified yet, or quota
        log(f"thumbnail not set ({exc.resp.status}); will retry next run")
        return False


def upload_captions(video_id: str, srt: pathlib.Path) -> bool:
    if MOCK:
        return True
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload
    try:
        service().captions().insert(part="snippet", body={"snippet": {
            "videoId": video_id, "language": CFG["channel"]["language"], "name": "English", "isDraft": False}},
            media_body=MediaFileUpload(str(srt), mimetype="application/octet-stream")).execute()
        return True
    except HttpError as exc:
        log(f"captions upload failed ({exc.resp.status}); will retry next run")
        return False


def unschedule(video_id: str) -> None:
    """Cancel a scheduled publish: stays private, publishAt removed."""
    if MOCK:
        return
    service().videos().update(part="status", body={"id": video_id, "status": _status("private")}).execute()


def schedule(video_id: str, publish_at_utc: str) -> None:
    """Keep private and let YouTube publish it at publish_at (RFC3339, UTC)."""
    if MOCK:
        return
    service().videos().update(part="status", body={"id": video_id, "status": _status("private", publish_at_utc)}).execute()


def recent_comments(limit: int = 60) -> list[str]:
    """Recent top-level viewer comments on this channel's videos (2-3 quota units). [] on any problem,
    e.g. before uploads are enabled or when the token is missing."""
    if MOCK:
        return []
    try:
        me = service().channels().list(part="id", mine=True).execute()
        channel_id = (me.get("items") or [{}])[0].get("id")
        if not channel_id:
            return []
        resp = service().commentThreads().list(part="snippet", allThreadsRelatedToChannelId=channel_id,
                                               maxResults=min(limit, 100), order="time",
                                               textFormat="plainText").execute()
        out = []
        for item in resp.get("items", []):
            top = item["snippet"]["topLevelComment"]["snippet"]
            if top.get("authorChannelId", {}).get("value") == channel_id:
                continue  # our own replies
            out.append(" ".join(str(top.get("textDisplay", "")).split())[:300])
        return [c for c in out if c]
    except Exception as exc:  # noqa: BLE001 - comments only help pick topics; never block the weekly list
        log(f"viewer comments unavailable: {str(exc)[:150]}")
        return []
