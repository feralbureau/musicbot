import asyncio
import logging
import os
from typing import Any, Dict, List, Optional, Union

import httpx

logger = logging.getLogger(__name__)

SOUNDCLOUD_API_V2_URL = "https://api-v2.soundcloud.com"
SOUNDCLOUD_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
SOUNDCLOUD_APP_VERSION = "1702458641"
_DEFAULT_HEADERS = {
    "User-Agent": SOUNDCLOUD_USER_AGENT,
    "Accept": "application/json",
}

_working_client_id: Optional[str] = None
_client_id_lock = asyncio.Lock()
_http_client: Optional[httpx.AsyncClient] = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)
    return _http_client


def _get_all_client_ids() -> List[str]:
    raw = os.getenv("SOUNDCLOUD_CLIENT_IDS", "")
    return [cid.strip() for cid in raw.split(",") if cid.strip()]


async def _get_working_client_id() -> Optional[str]:
    global _working_client_id
    async with _client_id_lock:
        if _working_client_id:
            return _working_client_id
        ids = _get_all_client_ids()
        if not ids:
            logger.error("No SOUNDCLOUD_CLIENT_IDS configured")
            return None
        client = _get_http_client()
        for cid in ids:
            try:
                resp = await client.get(
                    f"{SOUNDCLOUD_API_V2_URL}/search/tracks",
                    params={"q": "a", "limit": 1, "client_id": cid,
                            "app_version": SOUNDCLOUD_APP_VERSION, "app_locale": "en"},
                    headers=_DEFAULT_HEADERS,
                    timeout=5.0,
                )
                if resp.status_code == 200:
                    _working_client_id = cid
                    return cid
            except Exception:
                continue
        logger.error("All SoundCloud client IDs failed validation")
        return None


def _invalidate_client_id() -> None:
    global _working_client_id
    _working_client_id = None


async def _api_request(
    path: str,
    params: Optional[Dict] = None,
    max_retries: int = 1,
) -> Optional[httpx.Response]:
    if params is None:
        params = {}
    client = _get_http_client()
    for attempt in range(max_retries + 1):
        cid = await _get_working_client_id()
        if not cid:
            return None
        req_params = {
            **params,
            "client_id": cid,
            "app_version": SOUNDCLOUD_APP_VERSION,
            "app_locale": "en",
        }
        try:
            resp = await client.get(path, params=req_params, headers=_DEFAULT_HEADERS)
            if resp.status_code == 403:
                _invalidate_client_id()
                continue
            return resp
        except Exception as exc:
            logger.debug(f"SC request error ({path}): {exc}")
            _invalidate_client_id()
            if attempt >= max_retries:
                return None
    return None


def _artwork_url(url: Optional[str], size: str = "t500x500") -> Optional[str]:
    if not url:
        return None
    for marker in ["badge", "tiny", "small", "t67x67", "mini", "t120x120", "large", "t300x300", "crop"]:
        if f"-{marker}." in url:
            return url.replace(f"-{marker}.", f"-{size}.")
    return url if url.startswith("http") else None


def _format_duration(ms: Optional[int]) -> str:
    if not ms or ms <= 0:
        return "0:00"
    s = ms // 1000
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_track(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    track_id = raw.get("id")
    if not track_id:
        return None
    duration_ms = raw.get("duration", 0)
    artwork = _artwork_url(raw.get("artwork_url")) or _artwork_url(
        raw.get("user", {}).get("avatar_url")
    )
    streamable = any(
        t.get("format", {}).get("protocol") == "progressive"
        for t in raw.get("media", {}).get("transcodings", [])
    ) or raw.get("streamable", False)
    return {
        "id": str(track_id),
        "title": raw.get("title", "Unknown Title"),
        "artist": raw.get("user", {}).get("username", "Unknown Artist"),
        "duration": duration_ms // 1000,
        "duration_str": _format_duration(duration_ms),
        "streamable": streamable,
        "thumb": artwork,
        "webpage": raw.get("permalink_url", ""),
        "source": "soundcloud",
    }


async def resolve_url(url: str) -> Optional[Dict[str, Any]]:
    if "on.soundcloud.com" in url:
        client = _get_http_client()
        try:
            resp = await client.get(
                url,
                headers={"User-Agent": SOUNDCLOUD_USER_AGENT,
                         "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
                follow_redirects=True,
                timeout=10.0,
            )
            final = str(resp.url)
            if "soundcloud.com" in final and "on.soundcloud.com" not in final:
                url = final
            else:
                logger.warning(f"Short URL did not expand to soundcloud.com: {final}")
                return None
        except Exception as exc:
            logger.error(f"Short URL expand failed: {exc}")
            return None

    resp = await _api_request(f"{SOUNDCLOUD_API_V2_URL}/resolve", {"url": url})
    if not resp:
        return None
    try:
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.error(f"resolve_url error for {url[:60]}: {exc}")
        return None


async def search_tracks(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    limit = min(max(1, int(limit)), 50)
    resp = await _api_request(
        f"{SOUNDCLOUD_API_V2_URL}/search/tracks",
        {"q": query, "limit": limit, "offset": 0},
    )
    if not resp:
        return []
    try:
        resp.raise_for_status()
        data = resp.json()
        tracks = []
        for item in data.get("collection", []):
            t = format_track(item)
            if t:
                tracks.append(t)
        return tracks
    except Exception as exc:
        logger.error(f"search_tracks error for '{query[:40]}': {exc}")
        return []


async def get_stream_url(track_id: Union[str, int]) -> Optional[str]:
    if isinstance(track_id, str) and ":" in track_id:
        track_id = track_id.split(":")[-1]

    cid = await _get_working_client_id()
    if not cid:
        return None

    client = _get_http_client()
    headers = {
        "User-Agent": SOUNDCLOUD_USER_AGENT,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": "https://soundcloud.com/",
    }

    try:
        track_resp = await client.get(
            f"{SOUNDCLOUD_API_V2_URL}/tracks/{track_id}",
            params={"client_id": cid, "app_version": SOUNDCLOUD_APP_VERSION},
            headers=headers,
            timeout=10.0,
        )
        track_resp.raise_for_status()
        track_data = track_resp.json()
    except Exception as exc:
        logger.error(f"get_stream_url: track metadata fetch failed for {track_id}: {exc}")
        return None

    transcodings = track_data.get("media", {}).get("transcodings", [])
    stream_info_url: Optional[str] = None

    for t in transcodings:
        if t.get("format", {}).get("protocol") == "progressive" and t.get("url"):
            stream_info_url = t["url"]
            break

    if not stream_info_url:
        for t in transcodings:
            if t.get("format", {}).get("protocol") == "hls" and t.get("url"):
                stream_info_url = t["url"]
                break

    if not stream_info_url:
        logger.warning(f"No streamable transcoding found for track {track_id}")
        return None

    if "client_id=" not in stream_info_url:
        sep = "&" if "?" in stream_info_url else "?"
        stream_info_url += f"{sep}client_id={cid}"

    try:
        stream_resp = await client.get(stream_info_url, headers=headers, timeout=10.0)
        stream_resp.raise_for_status()
        final_url = stream_resp.json().get("url")
        if not final_url:
            logger.error(f"Stream response missing 'url' for track {track_id}")
            return None
        return final_url
    except Exception as exc:
        logger.error(f"get_stream_url: stream resolve failed for {track_id}: {exc}")
        return None


async def get_track(track_id: Union[str, int]) -> Optional[Dict[str, Any]]:
    if isinstance(track_id, str) and ":" in track_id:
        track_id = track_id.split(":")[-1]
    resp = await _api_request(f"{SOUNDCLOUD_API_V2_URL}/tracks/{track_id}")
    if not resp:
        return None
    try:
        resp.raise_for_status()
        return format_track(resp.json())
    except Exception as exc:
        logger.error(f"get_track error for {track_id}: {exc}")
        return None


async def get_related_tracks(track_id: Union[str, int], limit: int = 20) -> List[Dict[str, Any]]:
    if isinstance(track_id, str) and ":" in track_id:
        track_id = track_id.split(":")[-1]

    resp = await _api_request(
        f"{SOUNDCLOUD_API_V2_URL}/tracks/{track_id}/related",
        {"limit": min(int(limit), 50)},
    )
    if not resp:
        return []
    try:
        resp.raise_for_status()
        data = resp.json()
        candidates: list = []
        if isinstance(data, dict):
            candidates = data.get("collection") or data.get("tracks") or []
        elif isinstance(data, list):
            candidates = data
        tracks = []
        for item in candidates:
            t = format_track(item)
            if t and str(t["id"]) != str(track_id):
                tracks.append(t)
            if len(tracks) >= limit:
                break
        return tracks
    except Exception as exc:
        logger.error(f"get_related_tracks error for {track_id}: {exc}")
        return []
