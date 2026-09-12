// panel-grid.js -- the model, the VIRTUAL grid, zoom, and animation gating.
//
// ITEMS is the order and the selection; the DOM is a projection of the rows
// that happen to be near the viewport. This replaced a page that built every
// card up front: with 1 068 cards every drag step re-laid-out the whole grid
// (14-17 ms per pointer move, measured) and a scroll sweep dropped one frame in
// five. The row model here is arithmetic on fixed heights, so a render is a
// binary search plus ~100 node moves, whatever the catalog holds.
//
// panel-actions.js (loaded after this file) owns the gestures and the network;
// the shared state it needs -- `picked`, `carried`, `cards` -- is declared here.
'use strict';

const ITEMS = JSON.parse(document.getElementById('items-data').textContent);
const grid = document.getElementById('grid');
const padTop = document.getElementById('padTop');
const padBot = document.getElementById('padBot');
const park = document.getElementById('park');
const RM = matchMedia('(prefers-reduced-motion: reduce)').matches;
const cards = new Map();      // key -> card element, MOUNTED cards only
const picked = new Set();     // selection mode: keys that move together
const carried = new Set();    // keys the drag in progress is moving

// ---- geometry ------------------------------------------------------------
// JavaScript owns these numbers and hands them to CSS as variables, rounded
// to whole pixels: the spacer heights are sums of row heights, and a fractional
// row height would drift the window by a pixel per row over a long grid.
// The two heights are the MEASURED natural height of a card at zoom 1 (with
// and without the text rows) plus a pixel; too small clips the key line, too
// large is dead space in every row. Re-measure them after any change to the
// card's CSS: `card.style.height = 'auto'` and read `offsetHeight`.
const BASE = {cardW: 140, cardH: 262, cardHCompact: 190, sepH: 40, gap: 14};
const COMPACT_BELOW = 0.75;   // under this zoom the text rows are dropped
const ZOOM_MIN = 0.4, ZOOM_MAX = 2.4, ZOOM_STEP = 1.15;
let zoom = 1;
let G = {cols: 1, cardH: BASE.cardH, sepH: BASE.sepH, gap: BASE.gap};
let rows = [];                // {top, h, sep|start,end}
let rowOfItem = [];           // item index -> row index
let total = 0;                // height of every row plus the gaps between them
let gridTop = 0;              // document y of the first row
let headerH = 0;              // what the sticky header hides at the top
let win = {first: -1, last: -1};

function el(tag, cls, text){
  const n = document.createElement(tag);
  if(cls) n.className = cls;
  if(text !== undefined) n.textContent = text;
  return n;
}

function makeThumb(it){
  const box = el('div','thumb');
  const src = '/img/' + encodeURIComponent(it.key);
  if(it.fmt === 'video'){
    const v = el('video');
    v.muted = true; v.loop = true; v.playsInline = true;
    // Plays on its own, like the animated cards. Hover-only was rejected: a
    // grid of stills is useless for curating. Bounded the same way instead --
    // the viewport observer starts and pauses playback, and only a mounted
    // card even has a player, so what costs anything is what you can see.
    v.preload = 'metadata';
    v.dataset.play = '1';
    // No src yet: see videoIO. The URL waits on the element until the card is
    // about to be seen, so a mounted card costs no media player.
    v.dataset.src = src + '#t=0.001';
    box.appendChild(v);
  } else if(it.fmt === 'animated'){
    // An animated WebP, played by the browser itself. This used to be a
    // lottie.js SVG player per card (~704 DOM nodes each) which is what made
    // this panel crawl. One <img> animates on the compositor.
    const img = el('img');
    img.decoding = 'async';
    img.alt = it.label || '';
    // Starts as the still. The observer swaps in the animation when the card
    // is in the viewport -- an animated image the browser cannot show still
    // costs its decoded frames (~2.5 MB each here).
    const k = encodeURIComponent(it.key);
    img.dataset.anim = '/preview/' + k + '?fps=' + PREVIEW_FPS;
    img.dataset.still = '/preview/' + k + '?still=1';
    img.src = img.dataset.still;
    box.appendChild(img);
  } else {
    const img = el('img');
    // Not loading=lazy: a card is only built once its row is within half a
    // screen of the viewport, so the window IS the lazy loading, and a browser
    // heuristic on top of it only delayed the buffer rows until they popped.
    img.decoding = 'async';
    img.alt = it.label || '';     // property assignment: no attribute injection
    img.src = src;
    box.appendChild(img);
  }
  return box;
}

function makeCard(it){
  // fmt-* drives the colour: static, animated and video are told apart at a
  // glance instead of by reading the badge on every card.
  const card = el('div', it.isLogo ? 'card logo'
    : 'card fmt-' + it.fmt + ' ' + (it.included ? 'on' : 'off'));
  card.dataset.key = it.key;
  if(!it.isLogo) card.draggable = true;
  // One flex column, not three absolutely-positioned corners: a column cannot
  // overlap by construction. Number first, format under it, per owner request.
  const hdr = el('div','hdr');
  // Filled by render(), never here: a number written at build time is right
  // exactly once, and wrong from the first drag onwards.
  hdr.appendChild(el('span','pos',''));
  hdr.appendChild(el('span','badge', it.isLogo ? 'logo' : it.fmt));
  if(!it.isLogo) hdr.appendChild(el('span','tick', it.included ? '✓' : '✕'));
  if(!it.isLogo) hdr.appendChild(el('span','pick', '✓'));
  card.appendChild(hdr);
  card.appendChild(makeThumb(it));
  // The glyph the sticker carries. Telegram never shows it -- a custom emoji
  // renders as its picture -- so this grid is the only place it can be checked
  // against the art it is supposed to describe.
  const gl = el('div','glyph', it.emoji || '—');
  gl.title = it.emoji ? 'Glyph carried by this emoji: ' + it.emoji
                      : 'This emoji carries no glyph';
  card.appendChild(gl);
  const lbl = el('div','lbl', it.label || '');
  // copyId is decided server-side (panel.copy_id_for) so it is unit-tested.
  if(it.copyId){ lbl.classList.add('copyable'); lbl.dataset.copy = it.copyId;
                 lbl.title = 'Click to copy ' + it.copyId; }
  else if(it.label) lbl.title = it.label;   // the clamp may have cut it short
  card.appendChild(lbl);
  const sub = el('div','sub', it.isLogo
    ? 'always first, not part of the catalog'
    : it.key.slice(0,10) + '…');
  // It is the catalog's content key, not anything Telegram issued. People read
  // "a:65e766…" as an emoji id and it is not one -- it is our own hash of the
  // picture, which is what dedup and the media filename are keyed on.
  if(!it.isLogo) sub.title = 'Catalog content key (our hash of the picture): ' + it.key;
  card.appendChild(sub);
  return card;
}

// ---- pack boundaries -----------------------------------------------------
// Where each pack begins, computed from the INCLUDED items only: an unticked
// card never reaches Telegram, so it cannot push the next emoji into the
// following pack. That is why this is part of every layout and not just of a
// reorder -- untick enough cards and a boundary really does move.
function packStarts(){
  const logo = ITEMS.find(x=>x.isLogo);
  const capacity = PER_SET - (logo ? 1 : 0);   // items that fit beside the logo
  // An emoji already live in a pack knows WHICH pack (--with-pack sets it), and
  // then membership decides the boundaries. Capacity arithmetic cannot: two
  // published packs of 95 and 96 are neither of them PER_SET, so counting to
  // capacity finds no seam at all -- which is exactly how two packs came to
  // look like one long list.
  const byMembership = ITEMS.some(x => x.pack != null && x.included);
  const starts = [];                 // {index, pack}
  let inPack = 0, cur;
  for(let i = 0; i < ITEMS.length; i++){
    const it = ITEMS[i];
    // The logo IS emoji 0 of its pack, so it OPENS that pack's run. Excluding
    // it here put pack 5's marker one card below its own logo.
    if(byMembership){
      if(it.included){
        // A run ends when the pack number changes. A candidate carries no
        // pack and continues the run it was dropped into -- which is also
        // where it will publish -- so `cur` is deliberately left alone.
        const pk = it.pack != null ? it.pack : null;
        if(pk !== null && (!starts.length || pk !== cur)){
          starts.push({index: i, pack: pk});
          cur = pk;
        }
      }
    } else if(!it.isLogo && it.included){
      if(inPack === 0) starts.push({index: i, pack: null});
      inPack++;
      if(inPack >= capacity) inPack = 0;
    }
  }
  return {starts, logo};
}

// ---- the row model -------------------------------------------------------
// Rows are laid out in arithmetic: a separator row before each pack, then
// card rows of `cols` items -- a separator always starts a fresh row, exactly
// as CSS grid places a full-width item. Every offset is an integer.
function layoutRows(){
  const compact = zoom < COMPACT_BELOW;
  document.body.classList.toggle('compact', compact);
  G.gap  = Math.round(BASE.gap * zoom);
  G.cardH = Math.round((compact ? BASE.cardHCompact : BASE.cardH) * zoom);
  G.sepH = Math.round(BASE.sepH * zoom);
  const cs = getComputedStyle(grid);
  const inner = grid.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
  G.cols = Math.max(1, Math.floor((inner + G.gap) / (BASE.cardW * zoom + G.gap)));
  grid.style.setProperty('--cols', G.cols);
  grid.style.setProperty('--gap', G.gap + 'px');
  grid.style.setProperty('--cardH', G.cardH + 'px');
  grid.style.setProperty('--sepH', G.sepH + 'px');
  grid.style.setProperty('--z', zoom);

  const {starts, logo} = packStarts();
  const sepAt = new Map();           // item index -> the marker that precedes it
  // Only when there is more than one -- a single pack needs no divider.
  if(starts.length >= 2){
    for(let p = 0; p < starts.length; p++){
      // Pack 1 opens at the head logo, which already sits above its first item.
      const anchor = (p === 0 && logo) ? ITEMS.indexOf(logo) : starts[p].index;
      const to = p + 1 < starts.length ? starts[p + 1].index : ITEMS.length;
      const pk = starts[p].pack;
      sepAt.set(anchor, {title: pk != null ? 'Pack ' + pk : 'Pack ' + (p + 1),
                         from: starts[p].index + 1, to});
    }
  }
  const sepIdx = [...sepAt.keys()].sort((a,b)=>a-b);
  rows = []; rowOfItem = new Array(ITEMS.length);
  let y = 0, i = 0, s = 0;
  const N = ITEMS.length;
  while(i < N){
    if(sepAt.has(i)){
      rows.push({sep: sepAt.get(i), at: i, top: y, h: G.sepH});
      y += G.sepH + G.gap;
    }
    while(s < sepIdx.length && sepIdx[s] <= i) s++;
    const stop = s < sepIdx.length ? sepIdx[s] : N;
    const end = Math.min(i + G.cols, stop);
    rows.push({start: i, end, top: y, h: G.cardH});
    for(let k = i; k < end; k++) rowOfItem[k] = rows.length - 1;
    y += G.cardH + G.gap;
    i = end;
  }
  total = rows.length ? y - G.gap : 0;
  win = {first: -1, last: -1};        // the rows changed: the next render must project
}

function measure(){
  const r = grid.getBoundingClientRect();
  gridTop = r.top + scrollY + parseFloat(getComputedStyle(grid).paddingTop);
  const alert = document.getElementById('alert');
  const header = document.querySelector('header').offsetHeight;
  alert.style.top = header + 'px';     // the banner sticks just under the header
  headerH = header + (alert.classList.contains('show') ? alert.offsetHeight : 0);
}

// First row whose bottom edge is below `y`, and last row whose top is above.
function rowAt(y){
  let lo = 0, hi = rows.length - 1;
  while(lo < hi){
    const mid = (lo + hi) >> 1;
    if(rows[mid].top + rows[mid].h > y) hi = mid; else lo = mid + 1;
  }
  return lo;
}
function rowBefore(y){
  let lo = 0, hi = rows.length - 1;
  while(lo < hi){
    const mid = (lo + hi + 1) >> 1;
    if(rows[mid].top < y) lo = mid; else hi = mid - 1;
  }
  return lo;
}

const seps = new Map();       // title -> marker element, reused across renders
function sepNode(row){
  let box = seps.get(row.sep.title);
  if(!box){
    box = el('div','packsep');
    const logo = ITEMS.find(x=>x.isLogo);
    if(logo){
      const img = document.createElement('img');
      img.src = '/img/' + encodeURIComponent(logo.key);
      img.alt = '';
      box.appendChild(img);
    }
    box.appendChild(el('span','', row.sep.title));
    box.appendChild(el('span','n',''));
    seps.set(row.sep.title, box);
  }
  box.lastChild.textContent = `#${row.sep.from}–#${row.sep.to}`;
  return box;
}

function mount(k){
  const it = ITEMS[k];
  let c = cards.get(it.key);
  if(!c){
    c = makeCard(it);
    cards.set(it.key, c);
    for(const n of c.querySelectorAll('img[data-anim],video[data-play]')){
      if(animIO) animIO.observe(n);
      if(n.dataset.play){ if(videoIO) videoIO.observe(n); else attachVideo(n, true); }
    }
  }
  c.classList.toggle('picked', picked.has(it.key));
  c.classList.toggle('drag', carried.has(it.key));
  return c;
}

/** Take a card out of the document and out of `cards`. */
function unmountCard(c){
  cards.delete(c.dataset.key);
  for(const n of c.querySelectorAll('img[data-anim],video[data-play]')){
    if(animIO) animIO.unobserve(n);
    // A detached <video> keeps its player, and its decoder, until the garbage
    // collector gets round to it. Release it now: fifty-six of them alive at
    // once is what the old page paid for on every load.
    if(n.dataset.play){ if(videoIO) videoIO.unobserve(n); attachVideo(n, false); }
  }
  c.remove();
}

// A <video> costs a media player from the moment it has a source, and creating
// or tearing one down is the most expensive thing a card can do on the main
// thread -- measured as the 60-140 ms frames that were left once the grid was
// virtual. So a mounted video card carries only its URL; the source is attached
// when the card comes within a row of the viewport and released when it leaves.
const videoIO = window.IntersectionObserver ? new IntersectionObserver(es => {
  for (const e of es) attachVideo(e.target, e.isIntersecting);
}, {root: null, rootMargin: '300px'}) : null;

function attachVideo(v, on){
  try {
    if (on) { if (!v.getAttribute('src')) v.src = v.dataset.src; }
    else if (v.getAttribute('src')) { v.pause(); v.removeAttribute('src'); v.load(); }
  } catch(_){}
}

function setSpacer(node, h){
  if(h > 0){ node.style.height = h + 'px'; node.style.display = ''; }
  else node.style.display = 'none';
}

/** Project the rows near the viewport into the DOM. Safe to call often. */
function render(){
  if(!rows.length){
    let n = padTop.nextSibling;
    while(n && n !== padBot){ const s = n; n = n.nextSibling; retire(s); }
    setSpacer(padTop, 0); setSpacer(padBot, 0);
    win = {first: -1, last: -1};
    return;
  }
  // Half a screen of buffer each side: enough that a flick lands on rows that
  // already exist, small enough that a drag step re-lays-out ~100 cards.
  const viewTop = scrollY - gridTop;
  const pad = innerHeight / 2;
  let first = rowAt(viewTop - pad);
  let last = Math.max(first, rowBefore(viewTop + innerHeight + pad));
  // Scrolling within the same rows changes nothing; this runs once per frame
  // while scrolling, so the common case has to cost a binary search and no more.
  if(first === win.first && last === win.last) return;
  win = {first, last};

  const desired = [];
  for(let r = first; r <= last; r++){
    const row = rows[r];
    if(row.sep) desired.push(sepNode(row));
    else for(let k = row.start; k < row.end; k++) desired.push(mount(k));
  }
  // The spacers are grid rows of their own, so each also costs one gap.
  setSpacer(padTop, first > 0 ? rows[first].top - G.gap : 0);
  const bottom = rows[last].top + rows[last].h;
  setSpacer(padBot, bottom < total ? total - bottom - G.gap : 0);

  // Reconcile: walk the wanted order against what is there and move only
  // what differs. A node already in the document RELOCATES on insertBefore,
  // so a loaded thumbnail, a playing video, a decoded preview all survive.
  let cur = padTop.nextSibling;
  for(const n of desired){
    if(n === cur){ cur = cur.nextSibling; continue; }
    grid.insertBefore(n, cur);
  }
  while(cur && cur !== padBot){ const stale = cur; cur = cur.nextSibling; retire(stale); }

  // The grid number is the item's index -- ITEMS *is* the order, so there is
  // no second copy to drift. Only the mounted cards need writing.
  for(let r = first; r <= last; r++){
    const row = rows[r];
    if(row.sep) continue;
    for(let k = row.start; k < row.end; k++){
      const pos = cards.get(ITEMS[k].key).firstChild.firstChild;
      if(pos.textContent !== String(k + 1)) pos.textContent = k + 1;
    }
  }
}

function retire(node){
  if(!node.classList.contains('card')){ node.remove(); return; }
  // Never unmount a card the drag is carrying: dragend is delivered to the
  // source node, and a removed source leaves the gesture stuck. Park it; the
  // next dragover moves it back to wherever the pointer is.
  if(carried.has(node.dataset.key)) park.appendChild(node);
  else unmountCard(node);
}

/** Model changed (order, selection, zoom, width): recompute rows, re-project. */
function relayout(){ layoutRows(); render(); }

// ---- zoom ----------------------------------------------------------------
// The item at the top of the screen stays at the top of the screen: zooming
// is for seeing more or less of the same place, not for losing it.
function firstVisibleIndex(){
  if(!rows.length) return -1;
  let r = rowAt(scrollY + headerH - gridTop);
  while(r < rows.length && rows[r].sep) r++;
  return r < rows.length ? rows[r].start : -1;
}
function setZoom(z){
  z = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, z));
  if(z === zoom) return;
  const anchor = firstVisibleIndex();
  zoom = z;
  try{ localStorage.setItem('panelZoom', String(z)); }catch(_){}
  paintZoom();
  layoutRows();
  if(anchor >= 0) scrollTo(0, gridTop + rows[rowOfItem[anchor]].top - headerH);
  render();
}
function paintZoom(){
  document.getElementById('zoomReset').textContent = Math.round(zoom * 100) + '%';
}
function loadZoom(){
  let z = 1;
  try{ z = parseFloat(localStorage.getItem('panelZoom')) || 1; }catch(_){}
  zoom = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, z));
  paintZoom();
}
document.getElementById('zoomIn').onclick = ()=>setZoom(zoom * ZOOM_STEP);
document.getElementById('zoomOut').onclick = ()=>setZoom(zoom / ZOOM_STEP);
document.getElementById('zoomReset').onclick = ()=>setZoom(1);
// Ctrl+wheel is the browser's own page zoom; this takes it over for the grid,
// which is what anyone rolling the wheel with Ctrl held over a grid means.
// passive:false is what allows preventDefault to stop the browser zoom.
addEventListener('wheel', e=>{
  if(!e.ctrlKey) return;
  e.preventDefault();
  setZoom(e.deltaY < 0 ? zoom * ZOOM_STEP : zoom / ZOOM_STEP);
}, {passive: false});
addEventListener('keydown', e=>{
  if(!(e.ctrlKey || e.metaKey)) return;
  if(e.key === '=' || e.key === '+'){ e.preventDefault(); setZoom(zoom * ZOOM_STEP); }
  else if(e.key === '-' || e.key === '_'){ e.preventDefault(); setZoom(zoom / ZOOM_STEP); }
  else if(e.key === '0'){ e.preventDefault(); setZoom(1); }
});

// ---- scrolling and resizing ---------------------------------------------
// One render per frame at most, however many scroll events arrive.
let scrollRaf = null;
function scheduleRender(){
  if(scrollRaf !== null) return;
  scrollRaf = requestAnimationFrame(()=>{ scrollRaf = null; render(); });
}
addEventListener('resize', ()=>{ measure(); relayout(); });

// ---- animation gating ----------------------------------------------------
// Only the cards you can actually see animate. Everything else holds frame 0,
// so the number of live animations is bounded by the viewport rather than by
// the catalog. Swapping an <img> src is cheap -- both URLs are immutable-cached,
// so this never refetches -- which is what makes this affordable where
// mounting/destroying a player was not.
const animIO = window.IntersectionObserver ? new IntersectionObserver(es => {
  for (const e of es) {
    const t = e.target;
    const live = e.isIntersecting && ANIM_ON && !RM;
    if (t.dataset.play) { setPlaying(t, live); continue; }
    const want = live ? t.dataset.anim : t.dataset.still;
    if (want && t.getAttribute('src') !== want) t.src = want;
  }
}, {root: null, rootMargin: '0px'}) : null;   // see freezeAll(): a band
// beyond the viewport animated a row nobody was looking at, above AND below.

// play() rejects when the element is detached or the browser refuses; that is
// not an error worth surfacing, but it MUST be caught or it becomes an
// unhandled rejection on every scroll.
function setPlaying(v, on){
  if (on) attachVideo(v, true);      // playing implies a source, whichever observer spoke first
  try { if (on) { const q = v.play(); if (q) q.catch(()=>{}); } else { v.pause(); } }
  catch(_){}
}

function animatedNodes(){
  return grid.querySelectorAll('img[data-anim], video[data-play]');
}

/** Hold every mounted card on frame 0. Costs no request: the still is the
 *  same immutable-cached URL the card was built with. */
function freezeAll(){
  grid.querySelectorAll('video[data-play]').forEach(v => setPlaying(v, false));
  grid.querySelectorAll('img[data-anim]').forEach(img => {
    if (img.getAttribute('src') !== img.dataset.still) img.src = img.dataset.still;
  });
}

// Switching to another tab or window used to change nothing: every visible
// animation kept decoding for a page nobody was looking at. The browser
// throttles rAF for a hidden tab but not image animation, so this has to be
// explicit. Coming back re-evaluates visibility rather than assuming.
document.addEventListener('visibilitychange', ()=>{
  if (document.hidden) freezeAll(); else applyAnim();
});

// Scrolling is when a grid feels heavy, and it is the one moment the work is
// pure waste: the compositor is already busy, every visible animated WebP keeps
// decoding, and the frames go past too fast to see. Freeze on the first scroll
// event, thaw once it settles; the render itself is rAF-throttled.
let scrollThaw = null;
addEventListener('scroll', ()=>{
  scheduleRender();
  if(!ANIM_ON || RM) return;
  if(scrollThaw === null) freezeAll();
  else clearTimeout(scrollThaw);
  scrollThaw = setTimeout(()=>{ scrollThaw = null; applyAnim(); }, 180);
}, {passive:true});

// Master switch. Off = every card holds frame 0 and nothing decodes at all,
// which is the lightest the grid can be; the observer stops swapping so it
// cannot undo the freeze behind your back.
let ANIM_ON = localStorage.getItem('animOn') !== '0';
function applyAnim(){
  const btn = document.getElementById('anim');
  btn.setAttribute('aria-pressed', ANIM_ON ? 'true' : 'false');
  document.getElementById('animLabel').textContent =
    'Animation: ' + (ANIM_ON ? 'On' : 'Off');
  animatedNodes().forEach(n => {
    if (!ANIM_ON) {
      if (n.dataset.play) setPlaying(n, false);
      else if (n.getAttribute('src') !== n.dataset.still) n.src = n.dataset.still;
    } else if (animIO) { animIO.unobserve(n); animIO.observe(n); }  // re-evaluate
    else if (n.dataset.play) setPlaying(n, true);   // no observer: play what is mounted
  });
}

// ---- counters and per-card state ----------------------------------------
function updateCount(){
  // The logo ships in every set, so it counts. It is not toggleable, hence
  // always included.
  const logo = ITEMS.filter(x=>x.isLogo).length;
  const real = ITEMS.filter(x=>!x.isLogo);
  const included = real.filter(x=>x.included).length + logo;
  // Never silent: a grid that quietly drops 200 emoji is indistinguishable
  // from one that lost them.
  const note = document.getElementById('hiddenNote');
  if (note) {
    note.textContent = HIDDEN
      ? `· ${HIDDEN} in finished packs (hidden — panel.py --all shows them)`
      : '';
  }
  document.getElementById('selCount').textContent = included;
  document.getElementById('totCount').textContent = real.length + logo;
  // Telegram's hard cap is 200 stickers per set and the logo takes one of them,
  // so 200 chosen emoji plus the logo is 201 and CANNOT be one pack. Say so
  // here rather than let it be discovered as a surprise second set.
  const warn = document.getElementById('capWarn');
  if(included > PER_SET){
    const sets = Math.ceil(included / PER_SET);
    warn.textContent = `· ${included} > ${PER_SET} per set, so this publishes as `
                     + `${sets} packs (each led by the logo)`;
    warn.style.display = '';
  } else {
    warn.style.display = 'none';
  }
}
// In-place update of a MOUNTED card; an unmounted one is rebuilt from ITEMS
// when its row comes back, so it needs nothing.
function setCard(it){
  const el2 = cards.get(it.key); if(!el2) return;
  el2.classList.toggle('on',it.included); el2.classList.toggle('off',!it.included);
  const tick = el2.querySelector('.tick');
  if(tick) tick.textContent = it.included ? '✓' : '✕';
}
