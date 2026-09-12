// panel-actions.js -- gestures, history and the network. Loaded after
// panel-grid.js, which owns ITEMS, the row model and the mounted cards.
'use strict';

let lastIdx = null;

function setAll(fn){ remember();
  for(const it of ITEMS){ if(it.isLogo) continue; it.included = fn(it); setCard(it); }
  // relayout() too, not just the counter: unticking moves the pack splits.
  relayout(); updateCount(); }

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
grid.addEventListener('pointerdown', e=>{
  if(!selMode) return;
  const box = e.target.closest('.pick'); if(!box) return;
  const card = box.closest('.card');
  const i = ITEMS.findIndex(x=>x.key===card.dataset.key);
  if(i < 0 || ITEMS[i].isLogo) return;
  e.preventDefault();
  paintFrom = i;
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
  for(let k=a;k<=b;k++){ if(!ITEMS[k].isLogo) span.push(k); }
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
    for(let k=a;k<=b;k++){ if(ITEMS[k].isLogo) continue; ITEMS[k].included=val; setCard(ITEMS[k]); }
  } else {
    ITEMS[i].included=!ITEMS[i].included; setCard(ITEMS[i]);
  }
  lastIdx=i; relayout(); updateCount();
});

// --- Undo / redo ---------------------------------------------------------
// Snapshots, not a command log. A snapshot is the keys plus the included set,
// which is nothing, and it cannot drift out of step with ITEMS the way an
// inverse-operation log can -- and ITEMS is the only thing that decides what
// gets published. Bounded so a long session cannot grow without limit.
const HISTORY_MAX = 100;
let past = [], future = [];

function snapshot(){
  return {order: ITEMS.map(x=>x.key),
          included: ITEMS.filter(x=>x.included).map(x=>x.key)};
}

// Call BEFORE mutating, so the stack holds the state to return to -- or pass
// the snapshot that was taken before the mutation began, which is what a drag
// does, since its mutations start at the first dragover and end at the drop.
// A new action drops the redo branch, which is what every editor does.
function remember(snap){
  past.push(snap || snapshot());
  if(past.length > HISTORY_MAX) past.shift();
  future.length = 0;
  updateHistoryButtons();
}

function applyOrder(order){
  const pos = new Map(order.map((k,i)=>[k,i]));
  ITEMS.sort((a,b)=>pos.get(a.key)-pos.get(b.key));
}

function applySnapshot(snap){
  const inc = new Set(snap.included);
  for(const it of ITEMS) if(!it.isLogo) it.included = inc.has(it.key);
  applyOrder(snap.order);
  for(const it of ITEMS) if(!it.isLogo) setCard(it);
  // relayout() moves the mounted nodes rather than rebuilding them, so every
  // loaded thumbnail, preview and playing video survives an undo.
  relayout();
  updateCount();
  saveOrder();          // order is auto-saved; selection waits for Save, as always
  updateHistoryButtons();
}

function undo(){
  if(!past.length) return;
  future.push(snapshot());
  applySnapshot(past.pop());
  toast('Undone');
}

function redo(){
  if(!future.length) return;
  past.push(snapshot());
  applySnapshot(future.pop());
  toast('Redone');
}

function updateHistoryButtons(){
  document.getElementById('undo').disabled = !past.length;
  document.getElementById('redo').disabled = !future.length;
}

document.getElementById('undo').onclick = undo;
document.getElementById('redo').onclick = redo;
addEventListener('keydown', e=>{
  if(!(e.ctrlKey || e.metaKey)) return;
  const k = e.key.toLowerCase();
  if(k === 'z' && !e.shiftKey){ e.preventDefault(); undo(); }
  else if(k === 'y' || (k === 'z' && e.shiftKey)){ e.preventDefault(); redo(); }
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
let pendingOrder = null;      // an order we tried to save and could not
let lastAlert = '';
let orderTimer = null;

function setAlert(html){
  if(html === lastAlert) return;
  lastAlert = html;
  const a = document.getElementById('alert');
  a.innerHTML = html;
  a.classList.toggle('show', !!html);
  measure(); render();        // the banner pushes the grid down
}

function offline(why){
  setAlert('<b>Not saving.</b> ' + why +
           ' Your arrangement is only in this page — <b>do not close this tab.</b>' +
           ' It saves itself as soon as the panel is reachable again.');
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
  const send = () => fetch(path, {method:'POST',
    headers:{'Content-Type':'application/json','X-Panel-Token':TOK},
    body:JSON.stringify(body)});
  let r = await send();
  if(r.status === 403){
    const html = await (await fetch('/', {cache:'no-store'})).text();
    const m = /const TOKEN = "([^"]+)"/.exec(html);
    if(m){ TOK = m[1]; r = await send(); }
  }
  return r;
}

async function flushOrder(order){
  try{
    const r = await apiPost('/api/order', {order});
    if(!r.ok){ pendingOrder = order; offline('The panel refused the save (HTTP ' + r.status + ').'); return false; }
    pendingOrder = null;
    setAlert('');
    return true;
  }catch(_){
    pendingOrder = order;
    offline('The panel at this address is not responding.');
    return false;
  }
}

function saveOrder(){
  clearTimeout(orderTimer);
  const order = ITEMS.filter(x=>!x.isLogo).map(x=>x.key);
  pendingOrder = order;                 // at risk from this moment on
  orderTimer = setTimeout(async ()=>{
    if(await flushOrder(order)) toast('Order saved ✓');
  }, 400);
}

// Poll for the server rather than waiting for the next drag to discover it is
// gone -- the whole point is to find out while you can still act.
setInterval(async ()=>{
  try{
    const r = await fetch('/api/ping', {cache:'no-store'});
    if(!r.ok) throw new Error(r.status);
    if(pendingOrder) await flushOrder(pendingOrder);
    else setAlert('');
  }catch(_){
    offline('The panel process is not running.');
  }
}, 5000);

// Last line of defence: the browser asks before the tab takes the work with it.
addEventListener('beforeunload', e=>{
  if(pendingOrder){ e.preventDefault(); e.returnValue = ''; }
});

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
  commitDrag();
  endDrag(true);
});
grid.addEventListener('dragend',()=>endDrag(false));

/** Keep what the drag left in ITEMS: record where it started, save. */
function commitDrag(){
  const order = ITEMS.map(x=>x.key);
  if(order.every((k,i)=>k===dragSnap.order[i])) return;   // released where it started
  remember(dragSnap);
  saveOrder();
}

function endDrag(committed){
  // A cancelled drag has to put the order back: the model moved with the
  // pointer, so leaving it would make the grid disagree with what was saved.
  if(!committed && dragKey !== null){ applyOrder(dragSnap.order); relayout(); }
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
document.getElementById('all').onclick=()=>setAll(()=>true);
document.getElementById('none').onclick=()=>setAll(()=>false);
document.getElementById('inv').onclick=()=>setAll(x=>!x.included);
document.getElementById('anim').onclick=()=>{
  ANIM_ON = !ANIM_ON;
  try{ localStorage.setItem('animOn', ANIM_ON ? '1' : '0'); }catch(_){}
  applyAnim();
};
// Preview backdrop switcher: makes black / hollow / faint emoji visible.
const BGS=['checker','light','dark','gray'];
const BGLABEL={checker:'Checker',light:'Light',dark:'Dark',gray:'Gray'};
function applyBg(b){
  BGS.forEach(x=>document.body.classList.remove('bg-'+x));
  document.body.classList.add('bg-'+b);
  document.getElementById('bg').textContent='Backdrop: '+BGLABEL[b];
  try{localStorage.setItem('emojiBg',b);}catch(_){}
}
document.getElementById('bg').onclick=()=>{
  const cur=BGS.find(x=>document.body.classList.contains('bg-'+x))||'checker';
  applyBg(BGS[(BGS.indexOf(cur)+1)%BGS.length]);
};
document.getElementById('save').onclick=async()=>{
  const excluded = ITEMS.filter(x=>!x.isLogo && !x.included).map(x=>x.key);
  try{
    const r = await apiPost('/api/save', {excluded});
    const j = await r.json();
    if(r.ok){
      setAlert('');
      // Say WHOSE numbers these are. They come from the catalog, not the
      // grid, and reading "213 included" under 15 visible cards is
      // alarming until you know that.
      const scope = HIDDEN ? ' in the catalog' : '';
      toast(`Saved ✓  ${j.included} included · ${j.excluded} excluded${scope}`);
    }else{
      // Not a toast: a failed save you did not see is how an afternoon of
      // work goes missing.
      offline('The panel refused the save (' + (j.error || r.status) + ').');
    }
  }catch(_){ offline('The panel at this address is not responding.'); }
};
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
applyBg((()=>{try{return localStorage.getItem('emojiBg')||'checker';}catch(_){return 'checker';}})());
loadZoom();
measure();
relayout();
updateCount();
applyAnim();
