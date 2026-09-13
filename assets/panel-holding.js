// panel-holding.js -- park emoji outside the pack flow (2026-09-14).
//
// Three small, related additions that shipped together and are too small to
// each earn a file of their own:
//  - a holding tray: drag an emoji onto it to exclude it without hunting for
//    its tick in a 1000-card grid, drag one back out to place it in a pack at
//    an exact position. It is the SAME `included` flag the tick already
//    writes -- no new state, no backend change -- rendered in its own strip
//    instead of sitting wherever the card's position happens to be.
//  - Select all / Deselect all / Invert now drive the PICKS while selection
//    mode is on (pickAll/invertPicked below, wired from the existing buttons
//    in panel-actions.js). Before this they only ever touched publish
//    inclusion, so picking read as broken: nothing on screen responded.
//  - zoomReset is a typeable percentage now, not just a reset-to-100 button.
//
// panel-grid.js and panel-actions.js are both at or over the 700-line
// closed-to-new-code line, so nothing is added to either -- this file only
// READS their already-shared globals (ITEMS, cards, picked, selMode,
// carried, dragKey, dragSnap, remember, snapshot, relayout, setCard, toast,
// el) and wraps updateCount() once, which every include/exclude path
// already calls as its "the counts changed, repaint" signal.
'use strict';

// --- Select all / Invert, extended to the PICK set (selection mode) -------
function pickAll(){
  for(const it of ITEMS) if(!it.isLogo) markPicked(it.key, true);
  paintSelLabel();
}
function invertPicked(){
  for(const it of ITEMS) if(!it.isLogo) markPicked(it.key, !picked.has(it.key));
  paintSelLabel();
}

// --- Zoom: type an exact percentage ----------------------------------------
const zoomInput = document.getElementById('zoomReset');
function applyZoomInput(){
  const n = parseInt(zoomInput.value, 10);
  if(Number.isFinite(n) && n > 0) setZoom(n / 100);
  paintZoom();          // also redraws a value setZoom(1) left unchanged (e.g. typing "100")
  zoomInput.blur();
}
zoomInput.addEventListener('keydown', e=>{
  if(e.key === 'Enter'){ e.preventDefault(); applyZoomInput(); }
  else if(e.key === 'Escape'){ e.preventDefault(); paintZoom(); zoomInput.blur(); }
});
zoomInput.addEventListener('blur', applyZoomInput);
zoomInput.addEventListener('focus', ()=>zoomInput.select());
zoomInput.addEventListener('dblclick', ()=>setZoom(1));

// --- Holding area -----------------------------------------------------------
const holding = document.getElementById('holding');
const holdCards = document.getElementById('holdCards');
const holdCount = document.getElementById('holdCount');

function renderHolding(){
  const held = ITEMS.filter(x => !x.isLogo && !x.included);
  holdCount.textContent = held.length;
  // Rebuilt from scratch every time: this is a parking lot, not the grid, so
  // there is no thousand-card case here that would justify mount()'s reuse.
  holdCards.replaceChildren();
  for(const it of held){
    const c = el('div', 'hcard');
    c.draggable = true;
    c.dataset.key = it.key;
    c.title = it.label || it.key;
    const img = document.createElement('img');
    // still=1: a real frame for ANY format (static/video/animated), the same
    // still panel-actions.js uses nowhere else -- a tray thumbnail has no
    // business decoding or playing a video.
    img.src = '/preview/' + encodeURIComponent(it.key) + '?still=1';
    img.alt = '';
    img.loading = 'lazy';
    c.appendChild(img);
    holdCards.appendChild(c);
  }
  if(!held.length) holdCards.appendChild(el('span', 'hempty', 'Empty'));
}

// Every path that flips `included` already calls updateCount() as its
// "the counts changed, repaint" signal (click-toggle, setAll, undo/redo) --
// wrapping it once here catches all of them, including the first call at
// boot, without adding a line to panel-grid.js or panel-actions.js.
const _updateCount = updateCount;
updateCount = function(){ _updateCount(); renderHolding(); };

// -- Drag A: a card from the main grid, dropped ONTO the tray -> exclude ---
holding.addEventListener('dragover', e=>{
  if(dragKey === null) return;              // no card drag in progress
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  holding.classList.add('drop');
});
holding.addEventListener('dragleave', e=>{
  if(!holding.contains(e.relatedTarget)) holding.classList.remove('drop');
});
holding.addEventListener('drop', e=>{
  holding.classList.remove('drop');
  if(dragKey === null) return;
  e.preventDefault();
  remember();
  for(const k of carried){
    const it = ITEMS.find(x => x.key === k);
    if(it && !it.isLogo){ it.included = false; setCard(it); }
    const n = cards.get(k); if(n) n.classList.remove('drag');
  }
  // Cleared here, before dragend reaches the source card: grid's own dragend
  // reverts the order when it still sees a live drag, and parking an emoji
  // is not a reorder -- there is nothing to revert.
  carried.clear();
  dragKey = null;
  relayout(); updateCount(); markSelDirty();
});

// -- Drag B: a tray card, dropped back INTO the grid -> re-include there ---
// Reuses the grid's own dragover/drop for the reposition (dragKey/carried/
// dragSnap are the same shared state its dragstart would have set), so a
// held emoji lands exactly where dropped, in whichever pack that position
// falls into. Only the include-flip is this file's to do.
let holdDragKeys = null;
holdCards.addEventListener('dragstart', e=>{
  const card = e.target.closest('.hcard'); if(!card){ e.preventDefault(); return; }
  const key = card.dataset.key;
  // Selection mode's "carry the whole picked set" applies here too: picking
  // several held emoji and dragging one out releases all of them together.
  holdDragKeys = (selMode && picked.has(key))
    ? new Set(ITEMS.filter(x => picked.has(x.key) && !x.isLogo).map(x => x.key))
    : new Set([key]);
  dragKey = key;
  dragSnap = snapshot();       // so grid's own commitDrag has a real baseline
  carried.clear();
  for(const k of holdDragKeys) carried.add(k);
  e.dataTransfer.effectAllowed = 'move';
  try{ e.dataTransfer.setData('text/plain', key); }catch(_){}
});
holdCards.addEventListener('dragend', e=>{
  if(!holdDragKeys) return;
  // dropEffect is 'move' only when a drop target actually accepted it (the
  // grid's own dragover sets exactly that); 'none' means it was released
  // over nothing, so the emoji stays held, unchanged. A drop ON the grid
  // already repositioned and saved the order through its existing drop
  // handler -- this only ever needs to flip `included`.
  if(e.dataTransfer.dropEffect === 'move'){
    for(const k of holdDragKeys){
      const it = ITEMS.find(x => x.key === k);
      if(it){ it.included = true; setCard(it); }
    }
    relayout(); updateCount(); markSelDirty();
  }
  holdDragKeys = null;
  carried.clear();
  dragKey = null;
});

// --- Move every PICKED item to holding in one click (no drag required) ----
document.getElementById('toHold').onclick = () => {
  const keys = [...picked].filter(k => {
    const it = ITEMS.find(x => x.key === k);
    return it && !it.isLogo;
  });
  if(!keys.length){ toast('Pick something first'); return; }
  remember();
  for(const k of keys){
    const it = ITEMS.find(x => x.key === k);
    it.included = false; setCard(it);
  }
  clearPicked();
  relayout(); updateCount(); markSelDirty();
};

renderHolding();
