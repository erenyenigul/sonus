#!/usr/bin/env python3
"""
bcdl.py — Download Bandcamp album/track preview streams (128 kbps MP3)
and tag them using metadata from the page itself.

Usage:
    python3 bcdl.py <bandcamp-url> [-o OUTDIR]

Example:
    python3 bcdl.py https://milesdavis.bandcamp.com/album/bags-groove
    python3 bcdl.py https://milesdavis.bandcamp.com/track/doxy -o ~/Music

Notes:
    - Only grabs the free 128 kbps preview streams embedded in the page.
    - Signed URLs expire quickly; script fetches and downloads in one go.
    - Not all tracks are streamable; those are skipped with a warning.
"""

import argparse
import html
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests
from mutagen.id3 import (
    ID3, ID3NoHeaderError,
    TIT2, TPE1, TPE2, TALB, TRCK, TYER, TDRC,
    TCON, TPUB, TCOP, COMM, APIC,
)


USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def fetch_page(url: str) -> str:
    """GET the Bandcamp page HTML."""
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    return r.text


def extract_tralbum(page_html: str) -> dict:
    """Pull and parse the data-tralbum JSON blob from the page."""
    match = re.search(r'data-tralbum="([^"]+)"', page_html)
    if not match:
        raise RuntimeError(
            "Couldn't find data-tralbum on this page. "
            "Is it actually a Bandcamp album/track URL?"
        )
    return json.loads(html.unescape(match.group(1)))


def extract_jsonld(page_html: str) -> dict | None:
    """Pull the schema.org JSON-LD blob — has label, genre, keywords, etc."""
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


def extract_band_info(page_html: str) -> dict:
    """Pull the data-band attribute — has artist name and ID."""
    match = re.search(r'data-band="([^"]+)"', page_html)
    if not match:
        return {}
    try:
        return json.loads(html.unescape(match.group(1)))
    except json.JSONDecodeError:
        return {}


def safe_filename(name: str) -> str:
    """Strip characters that make filesystems unhappy."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.strip().rstrip(".")
    return name[:200] or "untitled"


def year_from_date(date_str: str | None) -> str | None:
    if not date_str:
        return None
    m = re.search(r"\b(19|20)\d{2}\b", date_str)
    return m.group(0) if m else None


def download_cover(art_id: int | None) -> bytes | None:
    """Fetch the large album cover from Bandcamp's image CDN."""
    if not art_id:
        return None
    url = f"https://f4.bcbits.com/img/a{art_id}_10.jpg"
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        r.raise_for_status()
        return r.content
    except requests.RequestException:
        return None


def download_stream(url: str, dest: Path) -> None:
    """Stream the MP3 to disk."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Range": "bytes=0-",
    }
    with requests.get(url, headers=headers, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    f.write(chunk)


def tag_mp3(
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
    """Write ID3v2 tags."""
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()

    tags.delall("TIT2"); tags.add(TIT2(encoding=3, text=title))
    tags.delall("TPE1"); tags.add(TPE1(encoding=3, text=artist))

    if album_artist:
        tags.delall("TPE2"); tags.add(TPE2(encoding=3, text=album_artist))
    if album:
        tags.delall("TALB"); tags.add(TALB(encoding=3, text=album))
    if track_num:
        trck = f"{track_num}/{total_tracks}" if total_tracks else str(track_num)
        tags.delall("TRCK"); tags.add(TRCK(encoding=3, text=trck))
    if year:
        tags.delall("TYER"); tags.add(TYER(encoding=3, text=year))
        tags.delall("TDRC"); tags.add(TDRC(encoding=3, text=year))
    if genre:
        tags.delall("TCON"); tags.add(TCON(encoding=3, text=genre))
    if label:
        tags.delall("TPUB"); tags.add(TPUB(encoding=3, text=label))
    if copyright_notice:
        tags.delall("TCOP"); tags.add(TCOP(encoding=3, text=copyright_notice))
    if comment:
        tags.delall("COMM")
        tags.add(COMM(encoding=3, lang="eng", desc="", text=comment))
    if cover_bytes:
        tags.delall("APIC")
        tags.add(APIC(
            encoding=3, mime="image/jpeg", type=3,
            desc="Cover", data=cover_bytes,
        ))

    tags.save(path, v2_version=3)


def gather_metadata(tralbum: dict, jsonld: dict | None, band: dict) -> dict:
    """Pull everything useful into one dict."""
    current = tralbum.get("current", {}) or {}
    artist = band.get("name") or (jsonld or {}).get("byArtist", {}).get("name") or "Unknown Artist"

    # Album vs single track page
    item_type = tralbum.get("item_type", "album")
    if item_type == "album":
        album_title = current.get("title") or (jsonld or {}).get("name")
    else:
        # On single-track pages, current.title is the track; album name (if any)
        # sometimes lives in album_title or via albumRelease in JSON-LD.
        album_title = current.get("album_title") or None

    # Genre + label from JSON-LD if present
    genre = None
    label = None
    copyright_notice = current.get("license_name") or None
    year = None

    if jsonld:
        # keywords is usually like ["Jazz", "New York"] — take the first that
        # looks like a genre (not a city). Bandcamp doesn't formally distinguish,
        # so just use the first keyword as a best-effort.
        kws = jsonld.get("keywords") or []
        if kws:
            genre = kws[0]

        # Year — prefer datePublished (release date) over dateModified
        year = year_from_date(jsonld.get("datePublished"))

        copyright_notice = jsonld.get("copyrightNotice") or copyright_notice

        # Record label is nested under the first albumRelease entry
        for rel in jsonld.get("albumRelease", []) or []:
            rl = rel.get("recordLabel")
            if rl and rl.get("name"):
                label = rl["name"]
                break

    # Fallback year from tralbum
    if not year:
        year = year_from_date(tralbum.get("album_release_date")) \
            or year_from_date(current.get("publish_date"))

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


def main():
    parser = argparse.ArgumentParser(
        description="Download Bandcamp preview MP3s with full tagging."
    )
    parser.add_argument("url", help="Bandcamp album or track URL")
    parser.add_argument(
        "-o", "--outdir", default=".",
        help="Output directory (default: current dir). A subfolder named "
             "'Artist - Album' will be created inside it for albums.",
    )
    args = parser.parse_args()

    parsed = urlparse(args.url)
    if not parsed.netloc.endswith("bandcamp.com"):
        print(f"warning: {parsed.netloc} doesn't look like a bandcamp URL, "
              "trying anyway", file=sys.stderr)

    print(f"Fetching {args.url} ...", file=sys.stderr)
    page = fetch_page(args.url)
    tralbum = extract_tralbum(page)
    jsonld = extract_jsonld(page)
    band = extract_band_info(page)
    meta = gather_metadata(tralbum, jsonld, band)

    trackinfo = tralbum.get("trackinfo") or []
    if not trackinfo:
        print("No tracks found on this page.", file=sys.stderr)
        sys.exit(1)

    # Output folder: <outdir>/<Artist> - <Album>/  for albums,
    # or just <outdir> for single-track pages.
    out_root = Path(args.outdir).expanduser()
    if meta["item_type"] == "album" and meta["album"]:
        folder_name = safe_filename(f"{meta['artist']} - {meta['album']}")
        out_dir = out_root / folder_name
    else:
        out_dir = out_root
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Artist : {meta['artist']}", file=sys.stderr)
    print(f"Album  : {meta['album'] or '(single track)'}", file=sys.stderr)
    print(f"Year   : {meta['year'] or '?'}", file=sys.stderr)
    print(f"Genre  : {meta['genre'] or '?'}", file=sys.stderr)
    print(f"Label  : {meta['label'] or '?'}", file=sys.stderr)
    print(f"Tracks : {len(trackinfo)}", file=sys.stderr)
    print(f"Output : {out_dir}", file=sys.stderr)
    print("", file=sys.stderr)

    print("Fetching cover art ...", file=sys.stderr)
    cover_bytes = download_cover(meta["art_id"])
    if cover_bytes:
        # Also save a copy of the cover next to the tracks for good measure
        (out_dir / "cover.jpg").write_bytes(cover_bytes)

    total = len(trackinfo)
    skipped = 0
    downloaded = 0

    for t in trackinfo:
        num = t.get("track_num") or 0
        title = t.get("title") or f"Track {num}"
        stream_url = (t.get("file") or {}).get("mp3-128")

        if not stream_url:
            print(f"  [{num:02d}] {title}  — no stream available, skipping",
                  file=sys.stderr)
            skipped += 1
            continue

        filename = safe_filename(f"{num:02d} - {title}.mp3") if num \
            else safe_filename(f"{title}.mp3")
        dest = out_dir / filename

        print(f"  [{num:02d}] {title}", file=sys.stderr)
        try:
            download_stream(stream_url, dest)
        except requests.RequestException as e:
            print(f"       ! download failed: {e}", file=sys.stderr)
            skipped += 1
            continue

        try:
            tag_mp3(
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
            print(f"       ! tagging failed: {e}", file=sys.stderr)

        downloaded += 1

    print("", file=sys.stderr)
    print(f"Done. {downloaded} downloaded, {skipped} skipped.", file=sys.stderr)


if __name__ == "__main__":
    main()