"""
Bandcamp download logic adapted from bcdl.py.
All progress is reported via a callback instead of printing to stderr.
"""

import html
import json
import re
from pathlib import Path
from typing import Callable

import requests
from mutagen.id3 import (
    ID3,
    TIT2, TPE1, TPE2, TALB, TRCK, TYER, TDRC,
    TCON, TPUB, TCOP, COMM, APIC,
)

Progress = Callable[[str], None]

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _fetch_page(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    return r.text


def _extract_tralbum(page_html: str) -> dict:
    match = re.search(r'data-tralbum="([^"]+)"', page_html)
    if not match:
        raise RuntimeError("Couldn't find data-tralbum — is this a Bandcamp album/track URL?")
    return json.loads(html.unescape(match.group(1)))


def _extract_jsonld(page_html: str) -> dict | None:
    match = re.search(
        r'<script type="application/ld\+json"[^>]*>(.*?)</script>',
        page_html,
        re.DOTALL,
    )
    if not match:
        return None
    try:
        return json.loads(match.group(1).strip())
    except json.JSONDecodeError:
        return None


def _extract_band_info(page_html: str) -> dict:
    match = re.search(r'data-band="([^"]+)"', page_html)
    if not match:
        return {}
    try:
        return json.loads(html.unescape(match.group(1)))
    except json.JSONDecodeError:
        return {}


def _safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.strip().rstrip(".")
    return name[:200] or "untitled"


def _year_from_date(date_str: str | None) -> str | None:
    if not date_str:
        return None
    m = re.search(r"\b(19|20)\d{2}\b", date_str)
    return m.group(0) if m else None


def _download_cover(art_id: int | None) -> bytes | None:
    if not art_id:
        return None
    url = f"https://f4.bcbits.com/img/a{art_id}_10.jpg"
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        r.raise_for_status()
        return r.content
    except requests.RequestException:
        return None


def _download_stream(url: str, dest: Path) -> None:
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*", "Range": "bytes=0-"}
    with requests.get(url, headers=headers, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    f.write(chunk)


def _tag_mp3(
    path: Path,
    *,
    title: str,
    artist: str,
    album: str | None,
    album_artist: str | None,
    track_num: int | None,
    total_tracks: int | None,
    year: str | None,
    genre: str | None,
    label: str | None,
    copyright_notice: str | None,
    comment: str | None,
    cover_bytes: bytes | None,
) -> None:
    tags = ID3()
    tags.add(TIT2(encoding=3, text=title))
    tags.add(TPE1(encoding=3, text=artist))
    if album_artist: tags.add(TPE2(encoding=3, text=album_artist))
    if album: tags.add(TALB(encoding=3, text=album))
    if track_num:
        trck = f"{track_num}/{total_tracks}" if total_tracks else str(track_num)
        tags.add(TRCK(encoding=3, text=trck))
    if year:
        tags.add(TYER(encoding=3, text=year))
        tags.add(TDRC(encoding=3, text=year))
    if genre: tags.add(TCON(encoding=3, text=genre))
    if label: tags.add(TPUB(encoding=3, text=label))
    if copyright_notice: tags.add(TCOP(encoding=3, text=copyright_notice))
    if comment: tags.add(COMM(encoding=3, lang="eng", desc="", text=comment))
    if cover_bytes: tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_bytes))
    tags.save(path, v2_version=3)


def _gather_metadata(tralbum: dict, jsonld: dict | None, band: dict) -> dict:
    current = tralbum.get("current", {}) or {}
    artist = (
        band.get("name")
        or (jsonld or {}).get("byArtist", {}).get("name")
        or "Unknown Artist"
    )

    item_type = tralbum.get("item_type", "album")
    if item_type == "album":
        album_title = current.get("title") or (jsonld or {}).get("name")
    else:
        album_title = current.get("album_title") or None

    genre = year = label = None
    copyright_notice = current.get("license_name") or None

    if jsonld:
        kws = jsonld.get("keywords") or []
        if kws:
            genre = kws[0]
        year = _year_from_date(jsonld.get("datePublished"))
        copyright_notice = jsonld.get("copyrightNotice") or copyright_notice
        for rel in jsonld.get("albumRelease", []) or []:
            rl = rel.get("recordLabel")
            if rl and rl.get("name"):
                label = rl["name"]
                break

    if not year:
        year = (
            _year_from_date(tralbum.get("album_release_date"))
            or _year_from_date(current.get("publish_date"))
        )

    return {
        "artist": artist,
        "album": album_title,
        "genre": genre,
        "label": label,
        "year": year,
        "copyright": copyright_notice,
        "art_id": tralbum.get("art_id") or current.get("art_id"),
        "album_url": tralbum.get("url"),
        "item_type": item_type,
    }


def download_url(url: str, out_root: Path, progress: Progress) -> dict:
    """Download a Bandcamp album or track to out_root. Returns metadata dict."""
    progress(f"Fetching page: {url}")
    page = _fetch_page(url)
    tralbum = _extract_tralbum(page)
    jsonld = _extract_jsonld(page)
    band = _extract_band_info(page)
    meta = _gather_metadata(tralbum, jsonld, band)

    trackinfo = tralbum.get("trackinfo") or []
    if not trackinfo:
        raise RuntimeError("No tracks found on this page.")

    if meta["item_type"] == "album" and meta["album"]:
        folder_name = _safe_filename(f"{meta['artist']} - {meta['album']}")
        out_dir = out_root / folder_name
    else:
        out_dir = out_root
    out_dir.mkdir(parents=True, exist_ok=True)

    progress(f"Artist: {meta['artist']}")
    progress(f"Album: {meta['album'] or '(single track)'}")
    progress(f"Year: {meta['year'] or '?'} | Genre: {meta['genre'] or '?'}")
    progress(f"Output: {out_dir}")
    progress(f"Fetching cover art...")

    cover_bytes = _download_cover(meta["art_id"])
    if cover_bytes:
        (out_dir / "cover.jpg").write_bytes(cover_bytes)
        progress("Cover saved.")

    total = len(trackinfo)
    downloaded = skipped = 0

    for t in trackinfo:
        num = t.get("track_num") or 0
        title = t.get("title") or f"Track {num}"
        stream_url = (t.get("file") or {}).get("mp3-128")

        if not stream_url:
            progress(f"  [{num:02d}] {title} — no stream, skipping")
            skipped += 1
            continue

        filename = (
            _safe_filename(f"{num:02d} - {title}.mp3") if num
            else _safe_filename(f"{title}.mp3")
        )
        dest = out_dir / filename
        progress(f"  [{num:02d}/{total}] Downloading: {title}")

        try:
            _download_stream(stream_url, dest)
        except requests.RequestException as e:
            progress(f"  [{num:02d}] Download failed: {e}")
            skipped += 1
            continue

        try:
            _tag_mp3(
                dest,
                title=title,
                artist=meta["artist"],
                album=meta["album"],
                album_artist=meta["artist"],
                track_num=num or None,
                total_tracks=total,
                year=meta["year"],
                genre=meta["genre"],
                label=meta["label"],
                copyright_notice=meta["copyright"],
                comment=f"Source: {meta['album_url']}" if meta["album_url"] else None,
                cover_bytes=cover_bytes,
            )
        except Exception as e:
            progress(f"  [{num:02d}] Tagging failed: {e}")

        downloaded += 1

    progress(f"Done: {downloaded} downloaded, {skipped} skipped.")
    return meta
