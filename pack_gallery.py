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
/* Deliberately the curate panel's visual language -- same grid, same card, same
   per-format accent, same checker backdrop -- so the two read as one product.
   What is NOT here is everything that MUTATES: no draggable, no tick, no save,
   no selection state. This page is for looking, and a control that looks live
   but changes nothing is worse than no control. */
:root{color-scheme:dark;--bg:#0b0f14;--panel:#111820;--line:#1f2a36;--txt:#e6edf6;
  --muted:#8fa0b8;--neon:#22d3ee;--neon2:#7dd3fc}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);
  font:14px/1.45 ui-sans-serif,system-ui,"Segoe UI",Roboto,sans-serif}
header{position:sticky;top:0;z-index:5;background:#0e141c;border-bottom:1px solid var(--line);
  padding:14px 20px}
h1{margin:0 0 4px;font-size:18px}
.meta{color:var(--muted);font-size:12px;line-height:1.5}
.meta a{color:var(--neon2)}
.meta b{color:var(--txt)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));
  gap:14px;padding:18px 20px 60px}
.card{position:relative;border:2px solid var(--line);border-radius:14px;
  background:var(--panel);padding:12px 10px 10px;text-align:center;
  /* Skip layout/paint off-screen: a coin pack is 200 cards and pack 1 is 200
     more, most of them animated. The size hint stops the scrollbar jumping. */
  content-visibility:auto;contain-intrinsic-size:auto 268px}
.card.fmt-static  {--fmt:#22d3ee;--fmtInk:#9fe8f5;--fmtBg:#06121b;--fmtLine:#1c3a44}
.card.fmt-animated{--fmt:#c4b5fd;--fmtInk:#ede9fe;--fmtBg:#140f28;--fmtLine:#4c3f7a}
.card.fmt-video   {--fmt:#34d399;--fmtInk:#a7f3d0;--fmtBg:#04170f;--fmtLine:#1b4a38}
.card.logo{border-color:#6b5316;--fmt:#fbbf24;--fmtInk:#fde68a;--fmtBg:#1a1508;--fmtLine:#5a4415}
.hdr{display:flex;flex-direction:column;align-items:center;gap:3px;margin:0 0 8px}
.pos{font-size:11px;font-weight:700;font-variant-numeric:tabular-nums;color:var(--neon2);
  background:#08131f;border:1px solid #1c3a44;border-radius:6px;padding:2px 6px;min-width:24px}
.badge{max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  font-size:10px;letter-spacing:.5px;text-transform:uppercase;color:var(--fmtInk,#9fd);
  background:var(--fmtBg,#06121b);border:1px solid var(--fmtLine,#1c3a44);
  border-radius:6px;padding:2px 5px}
.thumb{width:108px;height:108px;margin:3px auto 4px;border-radius:10px;display:flex;
  align-items:center;justify-content:center;overflow:hidden;
  outline:2px solid var(--fmt,#2b6f7d);outline-offset:2px;
  box-shadow:inset 0 0 0 1px #00000026;
  background-color:#828c9a;background-image:
    linear-gradient(45deg,#464e5a 25%,transparent 25%),
    linear-gradient(-45deg,#464e5a 25%,transparent 25%),
    linear-gradient(45deg,transparent 75%,#464e5a 75%),
    linear-gradient(-45deg,transparent 75%,#464e5a 75%);
  background-size:16px 16px;background-position:0 0,0 8px,8px -8px,-8px 0}
.thumb img,.thumb video{max-width:104px;max-height:104px;display:block}
.thumb.empty{background:#0a0e16;color:#61708a;font-size:11px}
/* The glyph the sticker carries. Invisible in Telegram -- only the picture is
   ever shown -- so the only place it can be checked against the art is here. */
.glyph{font-size:20px;line-height:1.1;margin:6px 0 2px}
.nm{font-size:11px;color:var(--muted);word-break:break-word;margin-bottom:6px}
/* Both ids copy on click. The source id is the one an external map may still
   point at, so it is as load-bearing as ours and gets the same affordance. */
.id{display:block;font:11px/1.35 ui-monospace,SFMono-Regular,Consolas,monospace;
  border-radius:6px;padding:3px 4px;margin-top:3px;cursor:copy;word-break:break-all;
  border:1px solid transparent}
.id:hover{border-color:var(--neon2);background:#0b1a26}
.id.now{color:#c9d6e8}
.id.was{color:#e0a86a}
.id .k{display:block;font-size:9px;letter-spacing:.06em;text-transform:uppercase;
  color:var(--muted)}
.card.copied .id.hit{border-color:#22c55e;color:#86efac}
"""

_COPY_JS = """
// Click either id to copy it. Nothing else on this page reacts to a click:
// it is a record, not an editor.
document.addEventListener('click', e => {
  const el = e.target.closest('.id');
  if (!el) return;
  const text = el.dataset.id || '';
  if (!text || !navigator.clipboard) return;
  navigator.clipboard.writeText(text).then(() => {
    const card = el.closest('.card');
    el.classList.add('hit'); card.classList.add('copied');
    setTimeout(() => { el.classList.remove('hit'); card.classList.remove('copied'); }, 700);
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
            thumb_cls = "thumb"
        else:
            art, thumb_cls = "no preview", "thumb empty"
        was = ", ".join(e["source_emoji_ids"])
        logo = e["role"] == "brand-logo"
        ids = (f'<span class="id now" data-id="{html.escape(cid)}" '
               f'title="click to copy"><span class="k">this pack</span>'
               f'{html.escape(cid)}</span>')
        if was:
            # The id it had where we took it from. External maps may still point
            # at it, so it is worth copying too -- not just worth reading.
            ids += (f'<span class="id was" data-id="{html.escape(was)}" '
                    f'title="click to copy"><span class="k">original pack</span>'
                    f'{html.escape(was)}</span>')
        cards.append(
            f'<div class="card {"logo" if logo else "fmt-" + html.escape(e["format"])}"'
            f' data-index="{e["index"]}" data-slot="{e["slot"]}"'
            f' data-custom-emoji-id="{html.escape(cid)}"'
            f' data-format="{html.escape(e["format"])}"'
            f' data-glyph="{html.escape(e["glyph"] or "")}"'
            f' data-source-emoji-ids="{html.escape(was)}">'
            f'<div class="hdr"><span class="pos">{e["index"]}</span>'
            f'<span class="badge">{"logo" if logo else html.escape(e["format"])}</span></div>'
            f'<div class="{thumb_cls}">{art}</div>'
            f'<div class="glyph" title="the glyph this sticker carries">'
            f'{html.escape(e["glyph"] or "—")}</div>'
            f'<div class="nm">{html.escape(e["name"] or "")}</div>'
            f'{ids}</div>')

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
