"""Turn one pack roster into a self-contained HTML page you can actually look at.

The JSON beside it answers "what is in this pack"; this answers "what does it
LOOK like", for a human and equally for a machine that reads pictures. So the
page is one file with nothing to fetch: every thumbnail is a ``data:`` URI, and
the whole roster is repeated verbatim in a ``<script type="application/json">``
block so a parser never has to scrape the markup it was given for looking at.

Animation survives without a single line of player code. An **animated WebP**
plays natively in every current browser, so a Lottie is rasterised once into one
(the same trick the curate panel uses) and a ``.webm`` emoji is embedded as
itself. No JS, no library, no CDN -- which is also why the page still works from
a file:// path years from now.

Thumbnails are cached in ``packs/.thumbs/<custom_emoji_id>.<ext>``. Rasterising
a 181-frame Lottie is ~300-500 ms and an estate refresh touches ~6600 emoji, so
without the cache a routine rebuild would cost most of an hour; with it, only
what actually changed is re-rendered. The key is the id rather than the file
path because a replaced sticker gets a NEW id, which is exactly when the picture
must be re-made.
"""
from __future__ import annotations

import base64
import html
import logging
from pathlib import Path

from PIL import Image

from emojikit import media

log = logging.getLogger("pack_gallery")

THUMB = 96          # px. Big enough to recognise, small enough that 200 of them
                    # inline stay a page rather than a download.
THUMB_FPS = 12
THUMB_QUALITY = 65
_MIME = {".webp": "image/webp", ".webm": "video/webm", ".png": "image/png"}


def _thumb_file(src: Path, fmt: str, dest_stem: Path) -> Path | None:
    """Rasterise one emoji to a small previewable file, or None if it cannot be.

    A missing or unreadable source is a hole in the page, never a failed run:
    an estate of thousands will always have one file the pipeline cannot open,
    and losing the other 6599 rows to it would be the wrong trade.
    """
    try:
        if fmt == "animated":
            out = dest_stem.with_suffix(".webp")
            media.lottie_preview_webp(src, out, size=THUMB, fps=THUMB_FPS,
                                      quality=THUMB_QUALITY)
            return out
        if fmt == "video":
            # Embedded as itself: 100x100 VP9 is already tiny, and re-encoding
            # would only cost quality for no size win.
            out = dest_stem.with_suffix(".webm")
            out.write_bytes(src.read_bytes())
            return out
        out = dest_stem.with_suffix(".webp")
        with Image.open(src) as im:
            im = im.convert("RGBA")
            im.thumbnail((THUMB, THUMB), Image.LANCZOS)
            # exact=True or libwebp rewrites the RGB under transparent pixels --
            # the same trap owner rule 1 meets on republished art.
            im.save(out, format="WEBP", quality=THUMB_QUALITY, exact=True)
        return out
    except Exception as exc:                      # noqa: BLE001 - one bad file must not lose the page
        log.warning("thumb failed for %s (%s): %s", src.name, fmt, exc)
        return None


def thumb_uri(cid: str, src: Path | None, fmt: str, cache: Path) -> tuple[str, str] | None:
    """``(data-uri, kind)`` for one emoji, rendering it once and reusing after."""
    if not src or not src.is_file():
        return None
    cache.mkdir(parents=True, exist_ok=True)
    stem = cache / cid
    existing = [p for p in (stem.with_suffix(".webp"), stem.with_suffix(".webm")) if p.is_file()]
    out = existing[0] if existing else _thumb_file(src, fmt, stem)
    if not out or not out.is_file():
        return None
    mime = _MIME.get(out.suffix, "application/octet-stream")
    b64 = base64.b64encode(out.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}", ("video" if out.suffix == ".webm" else "img")


_CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#0f1319;color:#dbe3ef;
  font:14px/1.45 ui-sans-serif,system-ui,"Segoe UI",Roboto,sans-serif}
header{position:sticky;top:0;z-index:2;background:#141a23;border-bottom:1px solid #223;
  padding:14px 20px}
h1{margin:0 0 4px;font-size:18px}
.meta{color:#8fa0b8;font-size:12px}
.meta a{color:#5aa9f0}
.grid{display:grid;gap:12px;padding:18px 20px;
  grid-template-columns:repeat(auto-fill,minmax(140px,1fr))}
.card{background:#161d27;border:1px solid #26303d;border-radius:12px;padding:10px;
  display:flex;flex-direction:column;align-items:center;gap:6px;text-align:center}
.card.logo{border-color:#6b5316;background:#1a1508}
.n{font:700 12px/1 ui-monospace,SFMono-Regular,Consolas,monospace;color:#7fd1ff}
.art{width:96px;height:96px;display:flex;align-items:center;justify-content:center;
  border-radius:10px;
  background-image:linear-gradient(45deg,#5c6672 25%,transparent 25%,transparent 75%,#5c6672 75%),
    linear-gradient(45deg,#5c6672 25%,#828c9a 25%,#828c9a 75%,#5c6672 75%);
  background-size:16px 16px;background-position:0 0,8px 8px}
.art img,.art video{max-width:96px;max-height:96px;display:block}
.art.empty{color:#61708a;font-size:11px;background:#10161f}
.cid{font:11px/1.3 ui-monospace,SFMono-Regular,Consolas,monospace;color:#c9d6e8;
  word-break:break-all;cursor:copy}
.cid:hover{color:#7fd1ff}
.nm{font-size:11px;color:#8fa0b8;word-break:break-word}
.was{font:10px/1.3 ui-monospace,SFMono-Regular,Consolas,monospace;color:#c08a5a;
  word-break:break-all}
.fmt{font-size:9px;letter-spacing:.06em;text-transform:uppercase;color:#7f8ea6}
"""

_COPY_JS = """
document.addEventListener('click', e => {
  const c = e.target.closest('.cid');
  if (!c || !navigator.clipboard) return;
  navigator.clipboard.writeText(c.textContent.trim()).then(() => {
    const was = c.textContent; c.textContent = 'copied';
    setTimeout(() => { c.textContent = was; }, 700);
  }, () => {});
});
"""


def render(doc: dict, media_of, cache: Path) -> str:
    """The page for one roster document.

    ``media_of(entry) -> Path | None`` is supplied by the caller because the two
    families keep their art in completely different places, and this module has
    no business knowing which is which.
    """
    cards, drawn = [], 0
    for e in doc["emoji"]:
        cid = e["custom_emoji_id"]
        got = thumb_uri(cid, media_of(e), e["format"], cache)
        if got:
            uri, kind = got
            art = (f'<video src="{uri}" autoplay loop muted playsinline></video>'
                   if kind == "video" else
                   f'<img src="{uri}" alt="emoji {e["index"]}" loading="lazy">')
            drawn += 1
            art_cls = "art"
        else:
            art, art_cls = "no preview", "art empty"
        was = ", ".join(e["source_emoji_ids"])
        cards.append(
            f'<div class="card{" logo" if e["role"] == "brand-logo" else ""}"'
            f' data-index="{e["index"]}" data-slot="{e["slot"]}"'
            f' data-custom-emoji-id="{html.escape(cid)}"'
            f' data-format="{html.escape(e["format"])}"'
            f' data-source-emoji-ids="{html.escape(was)}">'
            f'<span class="n">#{e["index"]} &middot; slot {e["slot"]}</span>'
            f'<div class="{art_cls}">{art}</div>'
            f'<span class="cid" title="click to copy">{html.escape(cid)}</span>'
            f'<span class="nm">{html.escape(e["name"] or "")} '
            f'{html.escape(e["glyph"] or "")}</span>'
            + (f'<span class="was">was {html.escape(was)}</span>' if was else "")
            + f'<span class="fmt">{html.escape(e["format"])}</span></div>')

    import json as _json
    title = html.escape(doc.get("title") or doc["set_name"])
    zero_note = (
        "<b>#</b> counts from 0 and emoji 0 is the brand logo; <b>slot</b> is the "
        "position Telegram shows. <b>was</b> is the id this emoji had in the pack "
        "it came from."
        if doc["family"] == "general" else
        "<b>#</b> counts from 0; <b>slot</b> is the position Telegram shows. This "
        "family carries no brand logo, so emoji 0 is a real coin.")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} - emoji roster</title>
<style>{_CSS}</style></head><body>
<header>
<h1>{title}</h1>
<div class="meta">
<code>{html.escape(doc["set_name"])}</code> &middot;
{doc["count"]} emoji ({drawn} with a preview) &middot;
{html.escape(doc["family"])} pack {doc.get("pack_index")} &middot;
<a href="{html.escape(doc["link"])}">{html.escape(doc["link"])}</a><br>
Captured {html.escape(doc["captured_utc"])} live from Telegram.
{zero_note}
</div></header>
<main class="grid">
{chr(10).join(cards)}
</main>
<script type="application/json" id="roster">
{_json.dumps(doc, ensure_ascii=False, indent=1)}
</script>
<script>{_COPY_JS}</script>
</body></html>
"""
