// panel-actions.js -- gestures and save queues. History lives in panel-holding.js.
// Loaded after
// panel-grid.js, which owns ITEMS, the row model and the mounted cards.
'use strict';

let lastIdx = null;

function setAll(fn){ remember();
  for(const it of ITEMS){ if(it.isLogo) continue; setIncluded(it, fn(it)); }
  // relayout() too, not just the counter: unticking moves the pack splits.
  relayout(); updateCount(); markSelDirty(); }

// Reduced motion is the one case where nothing plays by itself; hover is then
// the only way to see a video move at all, so the old handlers survive for it.
if (RM) {
  grid.addEventListener('mouseover',e=>{
    const box = e.target.closest('.thumb'); if(!box || box.contains(e.relatedTarget)) return;
    const v = box.querySelector('video'); if(v) setPlaying(v, true);
  });
  grid.addEventListener('mouseout',e=>{
    const box = e.target.closest('.thumb'); if(!box || box.contains(e.relatedTarget)) return;
    const v = box.querySelector('video'); if(v){ setPlaying(v, false); try{v.currentTime=0;}catch(_){} }
  });
}

// --- Selection mode ------------------------------------------------------
// Arranging a 200-card pack one emoji at a time is the slow part, so a run can
// be picked and carried in one gesture. It is a MODE rather than a modifier
// because the two things a card can be in -- shipped, and moving -- are
// different questions, and a chord that answers both is a chord that answers
// the wrong one by accident.
let selMode = false;

function markPicked(key, on){
  on ? picked.add(key) : picked.delete(key);
  const node = cards.get(key);        // unmounted cards pick up the class on mount
  if(node) node.classList.toggle('picked', on);
}
function clearPicked(){ for(const k of [...picked]) markPicked(k, false); paintSelLabel(); }
function paintSelLabel(){
  document.getElementById('selLabel').textContent =
    !selMode ? 'Selection: Off' : picked.size ? 'Selection: ' + picked.size + ' picked'
                                              : 'Selection: On';
}
document.getElementById('selmode').addEventListener('click', ()=>{
  remember();
  selMode = !selMode;
  document.body.classList.toggle('selmode', selMode);
  document.getElementById('selmode').setAttribute('aria-pressed', selMode ? 'true' : 'false');
  // Leaving the mode drops the picks: a hidden selection that still moves cards
  // on the next drag is the worst version of this feature.
  if(!selMode) clearPicked(); else paintSelLabel();
});

// Drag ACROSS the pick boxes to take a run. Pointer events, not HTML5 drag:
// the card's own draggable is what carries the set, and one element cannot do
// both gestures. Starting on the box is what tells them apart.
let paintFrom = null, paintTo = null;
let strokeBase = null;        // what was picked BEFORE this stroke
let strokeSpan = [];          // indices this stroke is currently claiming
let lastPick = null;          // a key survives reorders, unlike a remembered index
grid.addEventListener('pointerdown', e=>{
  if(!selMode) return;
  const box = e.target.closest('.pick'); if(!box) return;
  const card = box.closest('.card');
  const i = ITEMS.findIndex(x=>x.key===card.dataset.key);
  if(i < 0 || ITEMS[i].isLogo) return;
  e.preventDefault();
  remember();
  const anchor = ITEMS.findIndex(x=>x.key===lastPick && x.included);
  paintFrom = e.shiftKey && anchor >= 0 ? anchor : i;
  if(!e.shiftKey || anchor < 0) lastPick = ITEMS[i].key;
  paintTo = !picked.has(ITEMS[i].key);   // the whole stroke does what the first box did
  // The stroke is re-applied from this baseline on every move, so dragging BACK
  // shrinks the run instead of leaving whatever the pointer already passed.
  strokeBase = new Set(picked);
  strokeSpan = [];
  applyStroke(i);
  try{ box.setPointerCapture(e.pointerId); }catch(_){}
});
grid.addEventListener('pointermove', e=>{
  if(paintFrom === null) return;
  const under = document.elementFromPoint(e.clientX, e.clientY);
  const card = under && under.closest && under.closest('.card');
  if(!card) return;
  const j = ITEMS.findIndex(x=>x.key===card.dataset.key);
  if(j < 0) return;
  applyStroke(j);
});

function applyStroke(j){
  const [a,b] = [Math.min(paintFrom,j), Math.max(paintFrom,j)];
  const span = [];
  for(let k=a;k<=b;k++){ if(!ITEMS[k].isLogo && ITEMS[k].included) span.push(k); }
  const now = new Set(span);
  for(const k of strokeSpan){                    // released by dragging back
    if(!now.has(k)) markPicked(ITEMS[k].key, strokeBase.has(ITEMS[k].key));
  }
  for(const k of span) markPicked(ITEMS[k].key, paintTo);
  strokeSpan = span;
  paintSelLabel();
}
for(const ev of ['pointerup','pointercancel'])
  window.addEventListener(ev, ()=>{ paintFrom = null; });

grid.addEventListener('click',e=>{
  // Copying must not also toggle the card: the label sits inside it, so this
  // has to run first and stop there.
  const cp = e.target.closest('.copyable');
  if(cp){ e.stopPropagation(); copyText(cp.dataset.copy); return; }
  const card = e.target.closest('.card'); if(!card) return;
  // In selection mode the grid is for arranging only. A stray click must not
  // quietly drop an emoji from the pack while the owner is moving cards.
  if(selMode) return;
  const i = ITEMS.findIndex(x=>x.key===card.dataset.key);
  if(i < 0 || ITEMS[i].isLogo) return;   // preview-only card: not toggleable
  remember();
  if(e.shiftKey && lastIdx!==null){
    const [a,b]=[Math.min(lastIdx,i),Math.max(lastIdx,i)];
    const val = !ITEMS[i].included;
    for(let k=a;k<=b;k++){ if(ITEMS[k].isLogo) continue; setIncluded(ITEMS[k], val); }
  } else {
    setIncluded(ITEMS[i], !ITEMS[i].included);
  }
  lastIdx=i; relayout(); updateCount(); markSelDirty();
});

// --- Losing the server must never be silent ------------------------------
// An owner spent three hours reordering a pack while this process was already
// dead. The page looked fine, every drag "worked", and nothing reached the
// catalog. A toast was the only signal and it fades in 2.6 seconds.
//
// So: a banner that stays until the problem is actually gone, an unsaved
// change is remembered and flushed when the server comes back, and the browser
// asks before you close the tab on work that never landed.
let TOK = TOKEN;              // reissued per run; a restart invalidates ours
let lastAlert = '';

// Two queues, one for the order and one for the selection, each holding ONE
// outstanding state stamped with the revision that produced it. That stamp is
// the whole point: an acknowledgement may clear only the revision it
// acknowledges. Without it, an old reply spoke for work it had never carried
// -- a success cleared a newer arrangement outright, and a failure put its own
// stale snapshot back over one -- and the tab was then free to close on both.
let pendingOrder = null;      // newest arrangement not yet acknowledged
let orderRev = 0;             // the revision pendingOrder carries
let orderFlight = 0;          // revision in flight; 0 when idle (single flight)
let orderWait = null, orderBackoff = 0;

let pendingSel = null;        // immutable full body of the latest explicit Save
let selRev = 0, selFlight = 0, selWait = null, selBackoff = 0;

const RETRY_MIN = 1000, RETRY_MAX = 30000;

// One deadline per operation, covering the POST, the 403 token refresh and the
// body read. Without it a fetch that never settles left `orderFlight` set for
// the life of the page: `kickOrder` returns early while a flight is claimed, so
// every newer revision queued behind it silently stopped going.
const REQUEST_TIMEOUT = 15000;

// The earliest time each queue may try again. The 5-second heartbeat used to
// call flushOrder directly, which walked straight past the backoff -- eight
// heartbeats produced eight more identical POSTs after the first refusal. One
// scheduling authority means both the timer and the heartbeat ask this.
let orderNextAt = 0, selNextAt = 0;

// A refusal retrying cannot fix. 400 says the panel's catalog and this page
// have drifted apart, 409 that the page is too old to state its scope: both
// need reconciliation, and re-sending the same body just asks again. The work
// STAYS queued -- beforeunload still guards it -- but nothing resubmits it
// until the request itself changes.
const PERMANENT = new Set([400, 409]);
let orderStuck = false, selStuck = false;
let refusedOrder = null, refusedSel = null;
const orderSig = keys => keys.join('\0');

// What the SERVER has confirmed, as a comparable signature. Distinct from
// `pendingSel`, which is only ever "a Save that has not landed yet": edits made
// while a Save is in flight, or with no Save pressed at all, are unsaved work
// that neither of those told anyone about. An acknowledgement may retire the
// snapshot it carried and nothing newer.
const selSig = (keys) => keys.slice().sort().join('\0');
// Scope, exclusions AND intended packs are one Save, never a live-model retry.
function selectionBody(){
  const real = ITEMS.filter(x=>!x.isLogo);
  return {excluded: real.filter(x=>!x.included).map(x=>x.key),
          known: real.map(x=>x.key),
          packs: real.filter(x=>x.pack!=null).map(x=>[x.key,x.pack])};
}
function selectionSig(body){
  return JSON.stringify([body.known.slice().sort(), body.excluded.slice().sort(),
    body.packs.map(p=>p.slice()).sort((a,b)=>a[0]<b[0]?-1:a[0]>b[0]?1:0)]);
}
let ackedSel = selectionSig(selectionBody());

function currentExcluded(){
  return ITEMS.filter(x => !x.isLogo && !x.included).map(x => x.key);
}

/** Does the visible selection differ from what the server has acknowledged? */
function selDirty(){ return selectionSig(selectionBody()) !== ackedSel; }

/** Show it on the control you would press to fix it. */
function markSelDirty(){
  const b = document.getElementById('save');
  if(b) b.classList.toggle('dirty', selDirty());
}

function setAlert(html){
  if(html === lastAlert) return;
  lastAlert = html;
  const a = document.getElementById('alert');
  a.innerHTML = html;
  a.classList.toggle('show', !!html);
  measure(); render();        // the banner pushes the grid down
}

function offline(why){
  const safe = document.createElement('span'); safe.textContent = why;
  setAlert('<b>Not saving.</b> ' + safe.innerHTML +
           ' Your work is only in this page — <b>do not close this tab.</b>' +
           ' Eligible saves resume when the panel is reachable. <button id="exportDraft">Export draft</button>');
  document.getElementById('exportDraft').onclick=exportDraft;
}

/** Take the warning down only when there is nothing left to write.
 *
 *  A ping proves the process is alive, never that anything was stored, and a
 *  saved ORDER says nothing about a SELECTION that never landed. Both used to
 *  clear the banner, which is how a refused Save became invisible. */
function clearAlertIfClean(){
  if(pendingOrder === null && pendingSel === null) setAlert('');
}

/**
 * POST with the mutation token, refreshing it once on 403.
 *
 * The token is per run, so a restarted panel rejects ours. Re-reading it from
 * "/" is same-origin, which is exactly the boundary the token protects, so
 * this weakens nothing -- and it is what turns "restart the panel and lose
 * your afternoon" into "restart the panel and it catches up".
 */
async function apiPost(path, body){
  // ONE deadline for the whole operation. The body read is inside it because a
  // response whose stream never finishes wedges the queue exactly as a request
  // that never responds does, and only the caller's flight flag would ever
  // have noticed -- by staying set forever.
  const ac = new AbortController();
  let timer = 0;
  // RACED, not merely aborted. `AbortController` only helps if the transport
  // honours the signal, and the release of the flight flag must not depend on
  // that: a fetch that ignores it -- or resolves a body stream that never
  // ends -- would leave the queue claimed exactly as before. The abort is
  // still fired, so the real request is really cancelled; the rejection is
  // what guarantees the await settles.
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => {
      try{ ac.abort(); }catch(_){}
      reject(new Error('deadline'));
    }, REQUEST_TIMEOUT);
  });
  const bounded = (p) => Promise.race([p, deadline]);
  try{
    const send = () => fetch(path, {method:'POST',
      headers:{'Content-Type':'application/json','X-Panel-Token':TOK},
      body:JSON.stringify(body), signal:ac.signal});
    let r = await bounded(send());
    if(r.status === 403){
      // The token refresh is inside the same deadline: it is another network
      // round trip, and one that hangs strands the save just as surely.
      const res = await bounded(fetch('/', {cache:'no-store', signal:ac.signal}));
      const html = await bounded(res.text());
      const m = /const TOKEN = "([^"]+)"/.exec(html);
      if(m){ TOK = m[1]; r = await bounded(send()); }
    }
    let json;
    try{ json = await bounded(r.json()); }
    catch(err){
      // A successful status without a complete acknowledgement proves nothing.
      // Keep a non-success status useful even when its error body is malformed.
      if(r.ok) throw err;
      json = {};
    }
    const record = json !== null && typeof json === 'object' && !Array.isArray(json);
    if(r.ok){
      const count = n=>Number.isSafeInteger(n) && n>=0;
      if(r.status !== 200 || !record || json.ok !== true ||
         (path === '/api/save' && (!count(json.included) || !count(json.excluded))) ||
         (path === '/api/order' && !count(json.count))){
        throw new Error('invalid save acknowledgement');
      }
    }
    return {ok:r.ok, status:r.status, json:record?json:{}};
  } finally {
    clearTimeout(timer);
    deadline.catch(()=>{});     // nothing is listening once we are done
  }
}

/**
 * Send the pending arrangement, once.
 *
 * `order` is always `pendingOrder` -- the debounce and the heartbeat both
 * flush, and passing it explicitly is what lets an older snapshot handed in by
 * mistake be refused rather than written. A second caller while one is in
 * flight is a no-op: the reply re-arms the queue with whatever is newest by
 * then, so two saves can never race for the same catalog rows.
 */
async function flushOrder(order){
  if(orderFlight || orderStuck || Date.now()<orderNextAt || pendingOrder === null || order !== pendingOrder) return false;
  const rev = orderRev;
  orderFlight = rev;
  let r;
  try{
    r = await apiPost('/api/order', {order});
  }catch(_){
    return failOrder('The panel at this address is not responding.', 0, order);
  }
  if(!r.ok){
    return failOrder(PERMANENT.has(r.status)
      ? 'The panel rejected this arrangement — its catalog no longer matches '
        + 'this page. Export the draft before reconciling the catalog.'
      : 'The panel refused the save (HTTP ' + r.status + ').', r.status, order);
  }
  orderFlight = 0; orderBackoff = 0; orderStuck = false;
  logUI('save_succeeded',{revision:rev,count:order.length});
  // Only the acknowledged revision is saved. An arrangement made WHILE this was
  // in flight is still at risk and goes next, instead of being forgotten the
  // moment an older save came back ✓.
  if(rev !== orderRev){ kickOrder(0); return false; }
  pendingOrder = null;
  clearAlertIfClean();
  return true;
}

function failOrder(why, status, submitted){
  logUI('save_failed',{status,revision:orderFlight});
  orderFlight = 0;
  // The failed snapshot is deliberately NOT written back to pendingOrder: that
  // already holds the newest arrangement, which is this one or something later,
  // and restoring the old one is how a retry overwrote work that came after it.
  offline(why);
  if(PERMANENT.has(status)){
    // Its own comment already said retrying cannot fix a 400 -- and then it
    // scheduled a retry anyway, so a refusal repeated forever. The work stays
    // queued and guarded; what stops is the resubmitting.
    refusedOrder = orderSig(submitted);
    orderStuck = pendingOrder !== null && orderSig(pendingOrder)===refusedOrder;
    clearTimeout(orderWait);
    if(!orderStuck){orderBackoff=0;kickOrder(0);}
    return false;
  }
  orderStuck = false;
  orderBackoff = Math.min(RETRY_MAX, orderBackoff ? orderBackoff * 2 : RETRY_MIN);
  kickOrder(orderBackoff);
  return false;
}

function kickOrder(ms){
  clearTimeout(orderWait);
  if(pendingOrder === null || orderFlight || orderStuck) return;
  orderNextAt = Date.now() + ms;       // the heartbeat reads this too
  orderWait = setTimeout(()=>flushOrder(pendingOrder), ms);
}

function saveOrder(){
  pendingOrder = ITEMS.filter(x=>!x.isLogo).map(x=>x.key);
  orderRev++;                           // at risk from this moment on
  orderStuck = orderSig(pendingOrder)===refusedOrder;
  // The debounce uses the same eligibility gate as retry and heartbeat.
  if(!orderStuck){orderBackoff=0;kickOrder(400);}
}

// Poll for the server rather than waiting for the next drag to discover it is
// gone -- the whole point is to find out while you can still act.
setInterval(async ()=>{
  try{
    const r = await fetch('/api/ping', {cache:'no-store'});
    if(!r.ok) throw new Error(r.status);
    // Through the SAME schedule the timers use. Calling flushOrder directly
    // from here walked past the backoff entirely, so a refusal that had earned
    // a 30-second wait was re-sent every five seconds instead.
    const now = Date.now();
    if(pendingOrder !== null && !orderStuck && !orderFlight && now >= orderNextAt){
      await flushOrder(pendingOrder);
    }
    if(pendingSel !== null && !selStuck && !selFlight && now >= selNextAt){
      await flushSel(pendingSel);
    }
    clearAlertIfClean();
  }catch(_){
    offline('The panel process is not running.');
  }
}, 5000);

// Last line of defence: the browser asks before the tab takes the work with it.
// Either queue counts -- a Save that never landed loses exactly as much as an
// arrangement that never landed, and only the arrangement used to be asked
// about.
addEventListener('beforeunload', e=>{
  if(pendingOrder) return blockUnload(e);
  if(pendingSel) return blockUnload(e);
  // And ordinary unsaved ticks, which neither queue knows about. `pendingSel`
  // means "a Save is still trying"; it says nothing about edits made while one
  // was in flight, or about edits where Save was never pressed. Both are work
  // this tab would take with it.
  if(selDirty()) blockUnload(e);
});
function blockUnload(e){ e.preventDefault(); e.returnValue = ''; }

// --- Drag & drop reordering (sets the publish order) --------------------
// The MODEL is edited as you drag: every dragover that changes the target
// moves the carried items inside ITEMS and re-projects the grid, so what you
// see IS where they land. The drop only records the result; a cancel puts the
// order taken at dragstart back. There is no index arithmetic at drop time --
// the old scheme measured a `to` before the removal and landed one slot off
// in one direction -- and nothing for a preview and a commit to disagree
// about, because the grid is never anything but ITEMS drawn.
let dragKey = null;
let dragSnap = null;          // the order at dragstart: history on commit, restore on cancel

grid.addEventListener('dragstart',e=>{
  const card=e.target.closest('.card'); if(!card){e.preventDefault();return;}
  const it = ITEMS.find(x=>x.key===card.dataset.key);
  if(!it || it.isLogo){ e.preventDefault(); return; }   // logo is fixed first
  dragKey=card.dataset.key;
  dragSnap=snapshot();
  // Dragging a PICKED card carries the whole set; dragging an unpicked one is
  // the single-card gesture that has always been here, untouched.
  carried.clear();
  const keys = (selMode && picked.has(dragKey))
    ? ITEMS.filter(x=>picked.has(x.key) && !x.isLogo).map(x=>x.key)
    : [dragKey];
  for(const k of keys){ carried.add(k); const n = cards.get(k); if(n) n.classList.add('drag'); }
  e.dataTransfer.effectAllowed='move';
  try{e.dataTransfer.setData('text/plain',dragKey);}catch(_){}
});

grid.addEventListener('dragover',e=>{
  if(dragKey===null) return;
  e.preventDefault(); e.dataTransfer.dropEffect='move';
  const card=e.target.closest('.card');
  if(!card || carried.has(card.dataset.key)) return;   // over a card being carried
  const j = ITEMS.findIndex(x=>x.key===card.dataset.key);
  if(j < 0 || ITEMS[j].isLogo) return;      // never ahead of the brand logo
  // Which SIDE of the tile the pointer is on decides before-or-after, so the
  // last slot of a row is reachable and the gesture reads the way it looks.
  const r = card.getBoundingClientRect();
  moveCarried((e.clientX > r.left + r.width/2) ? j + 1 : j);
});

/** Put the carried run in front of the item now at `slot`, keeping its own
 *  internal order however far it travels. No-op when it is already there. */
function moveCarried(slot){
  const moving = [], rest = [];
  let at = slot;
  for(let i = 0; i < ITEMS.length; i++){
    const it = ITEMS[i];
    if(carried.has(it.key)){ moving.push(it); if(i < slot) at--; }
    else rest.push(it);
  }
  const next = rest.slice(0, at).concat(moving, rest.slice(at));
  if(next.every((x,i)=>x===ITEMS[i])) return;
  ITEMS.length = 0; ITEMS.push(...next);
  relayout();
}

grid.addEventListener('drop',e=>{
  if(dragKey===null) return;
  e.preventDefault();
  stopEdgeScroll();
  if(!acceptHeldDrop()){endDrag(false);return;}
  commitDrag();
  endDrag(true);
});
grid.addEventListener('dragend',()=>endDrag(false));

/** Keep what the drag left in ITEMS: record where it started, save. */
function commitDrag(){
  const order = ITEMS.map(x=>x.key);
  if(order.every((k,i)=>k===dragSnap.order[i]) &&
     selSig(ITEMS.filter(x=>x.included).map(x=>x.key))===selSig(dragSnap.included)) return;
  remember(dragSnap);
  logUI('reorder',{count:carried.size});
  saveOrder();
}

function endDrag(committed){
  // A cancelled drag has to put the order back: the model moved with the
  // pointer, so leaving it would make the grid disagree with what was saved.
  if(!committed && dragKey !== null){ applySnapshot(dragSnap,false); }
  for(const k of carried){ const n = cards.get(k); if(n) n.classList.remove('drag'); }
  carried.clear(); dragKey=null; dragSnap=null;
  stopEdgeScroll();
  // Anything still parked is outside the window now that nothing carries it.
  while(park.firstChild) unmountCard(park.firstChild);
}

// --- Auto-scroll while dragging near an edge ----------------------------
// Without this the drag is trapped in the current viewport: with 200 cards
// there is no way to carry #200 up to #10, because the page will not follow
// the pointer. Speed rises the deeper into the edge band you go, so a nudge
// creeps and a hard push travels.
const EDGE_BAND = 100;      // px from the top/bottom edge where scrolling starts
const EDGE_MAX  = 42;       // px per frame at the very edge
let edgeSpeed = 0, edgeFrame = null;

function edgeScroll(y){
  const over = EDGE_BAND - y;                       // >0 once inside the top band
  const under = y - (innerHeight - EDGE_BAND);      // >0 once inside the bottom band
  const depth = over > 0 ? -over : (under > 0 ? under : 0);
  edgeSpeed = Math.max(-EDGE_MAX, Math.min(EDGE_MAX,
                       Math.round(depth / EDGE_BAND * EDGE_MAX)));
  if(edgeSpeed && edgeFrame === null) stepEdge();
}

function stepEdge(){
  edgeFrame = requestAnimationFrame(()=>{
    edgeFrame = null;
    // Guarded on dragKey as well: a drag that ends outside the window never
    // fires drop, and an unguarded loop would scroll the page forever.
    if(dragKey === null || !edgeSpeed) return;
    scrollBy(0, edgeSpeed);
    stepEdge();
  });
}

function stopEdgeScroll(){
  edgeSpeed = 0;
  if(edgeFrame !== null){ cancelAnimationFrame(edgeFrame); edgeFrame = null; }
}

// On the DOCUMENT, not the grid. Grid events bubble here anyway, and at the top
// of the window the pointer is over the sticky header -- where the grid's own
// handler never fires, which is exactly when scrolling up is wanted.
document.addEventListener('dragover',e=>{
  if(dragKey===null) return;
  e.preventDefault();
  edgeScroll(e.clientY);
});
document.addEventListener('dragend',()=>endDrag(false));
// A drop anywhere outside the grid: cancel rather than reorder by guesswork.
document.addEventListener('drop',e=>{
  if(dragKey===null) return;
  e.preventDefault();
  if(!e.target.closest('#grid')) endDrag(false);
});

// Plain document scrolling, NOT scrollIntoView. scrollIntoView aligns the
// element with the top of the VIEWPORT, which is behind the sticky header, so
// "Top" stopped one header short and hid the Pack 1 marker; the same alignment
// rule cut the bottom short. The document ends where the last row does -- the
// bottom spacer sees to that -- and the header floats above it.
document.getElementById('top').onclick=()=>window.scrollTo({top:0});
document.getElementById('bot').onclick=()=>
  window.scrollTo({top:document.documentElement.scrollHeight});
document.getElementById('all').onclick=()=>selMode ? pickAll() : setAll(()=>true);
document.getElementById('none').onclick=()=>{ if(selMode){remember();clearPicked();}else setAll(()=>false); };
document.getElementById('inv').onclick=()=>selMode ? invertPicked() : setAll(x=>!x.included);
document.getElementById('anim').onclick=()=>{
  remember();
  ANIM_ON = !ANIM_ON;
  prefs.set('animOn', ANIM_ON ? '1' : '0');
  applyAnim();
};
// Preview backdrop switcher: makes black / hollow / faint emoji visible.
const BGS=['checker','light','dark','gray'];
const BGLABEL={checker:'Checker',light:'Light',dark:'Dark',gray:'Gray'};
function applyBg(b){
  // A stored value can be anything -- an older build's name, or a profile that
  // hands back a string nobody here wrote. Unknown means the default, not a
  // backdrop class that matches no rule and a label reading "undefined".
  if(!BGS.includes(b)) b = 'checker';
  BGS.forEach(x=>document.body.classList.remove('bg-'+x));
  document.body.classList.add('bg-'+b);
  document.getElementById('bg').textContent='Backdrop: '+BGLABEL[b];
  prefs.set('emojiBg', b);
}
document.getElementById('bg').onclick=()=>{
  remember();
  const cur=BGS.find(x=>document.body.classList.contains('bg-'+x))||'checker';
  applyBg(BGS[(BGS.indexOf(cur)+1)%BGS.length]);
};
// Selection persists only when Save is pressed. That is the contract, and a
// retry must not quietly widen it: a refused Save keeps the snapshot it was
// GIVEN and retries exactly that, so ticks made afterwards stay unsaved until
// the owner presses Save again -- the same as if the failure had never
// happened. What did change is that the refusal is no longer forgotten.
async function flushSel(submitted){
  if(selFlight || selStuck || Date.now()<selNextAt || pendingSel === null || submitted !== pendingSel) return false;
  const rev = selRev;
  const checkpoint = pendingSaveSnapshot;
  selFlight = rev;
  let r;
  // Nothing is re-read from ITEMS here: a retry is the same explicitly saved
  // scope, selection and pack intent, even after newer unsaved edits.
  try{ r = await apiPost('/api/save', submitted); }
  catch(_){ return failSel('The panel did not provide a valid save acknowledgement.', 0, submitted); }
  const j = r.json;
  if(!r.ok){
    return failSel('The panel refused the save (' + (j.error || r.status) + ').',
                   r.status, submitted);
  }
  selFlight = 0; selBackoff = 0; selStuck = false;
  ackedSel = selectionSig(submitted);
  logUI('save_succeeded',{revision:rev,count:submitted.excluded.length});
  if(checkpoint) savedSnapshot = checkpoint;
  markSelDirty();
  if(rev !== selRev){ kickSel(0); return false; }
  pendingSel = null;
  clearAlertIfClean();
  const scope = HIDDEN ? ' in the catalog' : '';
  toast(selDirty() ? 'Saved the submitted version; newer changes are unsaved.'
    : `Saved ✓  ${j.included} included · ${j.excluded} excluded${scope}`);
  return true;
}

function failSel(why, status, submitted){
  logUI('save_failed',{status,revision:selFlight});
  selFlight = 0;
  // Not a toast: a failed save you did not see is how an afternoon of work
  // goes missing. The banner stays up until the save actually lands, and the
  // snapshot stays queued so it still can.
  offline(why);
  if(PERMANENT.has(status)){
    // A 409 means this page is too old to say what it was showing, and a 400
    // that the body itself is wrong. Re-sending the same body gets the same
    // answer; the selection stays queued and guarded until a reconciled tab
    // saves it.
    refusedSel = selectionSig(submitted);
    selStuck = pendingSel !== null && selectionSig(pendingSel)===refusedSel;
    clearTimeout(selWait);
    if(!selStuck){selBackoff=0;kickSel(0);}
    return false;
  }
  selStuck = false;
  selBackoff = Math.min(RETRY_MAX, selBackoff ? selBackoff * 2 : RETRY_MIN);
  kickSel(selBackoff);
  return false;
}

function kickSel(ms){
  clearTimeout(selWait);
  if(pendingSel === null || selFlight || selStuck) return;
  selNextAt = Date.now() + ms;
  selWait = setTimeout(()=>flushSel(pendingSel), ms);
}

function queueSelection(checkpoint){
  pendingSel = selectionBody();
  // Snapshot arrays have no references into the mutable item model. Copy the
  // checkpoint as well so Reset always describes the acknowledged Save.
  pendingSaveSnapshot = JSON.parse(JSON.stringify(checkpoint));
  selRev++;
  logUI('save_requested',{revision:selRev,count:pendingSel.excluded.length});
  selStuck = selectionSig(pendingSel)===refusedSel;
  if(!selStuck){selBackoff=0;selNextAt=Date.now();}
  flushSel(pendingSel);
}
document.getElementById('save').onclick=()=>queueSelection(snapshot());
async function copyText(text){
  if(!text) return;
  // The panel is served from a loopback host, which IS a secure context, so
  // the async clipboard API is normally available. It still REJECTS in real
  // situations -- "Document is not focused" is the one this hit in testing --
  // so a rejection falls through to execCommand rather than giving up.
  if(navigator.clipboard && window.isSecureContext){
    try{ await navigator.clipboard.writeText(text); toast('Copied ' + text); return; }
    catch(_){ /* fall through */ }
  }
  let ok = false;
  try{
    const ta = document.createElement('textarea');
    ta.value = text; ta.setAttribute('readonly','');
    ta.style.cssText = 'position:fixed;top:-1000px';
    document.body.appendChild(ta);
    ta.select(); ta.setSelectionRange(0, text.length);
    ok = document.execCommand('copy');
    ta.remove();
  }catch(_){ ok = false; }
  // Never claim a copy that did not happen: the user would paste whatever was
  // on the clipboard before and not know why it was wrong.
  toast(ok ? 'Copied ' + text : 'Could not copy — select and copy manually');
}

function toast(msg){const t=document.getElementById('toast');t.textContent=msg;
  t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2600);}

// --- boot ----------------------------------------------------------------
applyBg(prefs.get('emojiBg', 'checker'));
loadZoom();
measure();
relayout();
updateCount();
markSelDirty();   // the page loads showing exactly what the server has
applyAnim();
