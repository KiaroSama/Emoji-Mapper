// Curation history, holding and explicit Save checkpoints share one model.
'use strict';

const HISTORY_MAX = 100;
let past = [], future = [], restoring = false;
const holdOrigins = new Map();
let savedSnapshot = null, pendingSaveSnapshot = null;

function snapshot(){
  return {order: ITEMS.map(x=>x.key), included: ITEMS.filter(x=>x.included).map(x=>x.key),
    packs: ITEMS.filter(x=>x.pack!=null).map(x=>[x.key,x.pack]),
    picked: [...picked], selMode, lastPick, lastIdx, holds: [...holdOrigins],
    zoom, anim: ANIM_ON, bg: BGS.find(x=>document.body.classList.contains('bg-'+x)) || 'checker'};
}
function remember(snap){
  if(restoring) return;
  const state = snap || snapshot();
  if(!past.length || JSON.stringify(past[past.length-1]) !== JSON.stringify(state)) past.push(state);
  if(past.length > HISTORY_MAX) past.shift();
  future.length = 0;
  updateHistoryButtons();
}
function applyOrder(order){
  const pos = new Map(order.map((k,i)=>[k,i]));
  ITEMS.sort((a,b)=>pos.get(a.key)-pos.get(b.key));
}
function applySnapshot(snap, persist=true){
  const before = ITEMS.filter(x=>!x.isLogo).map(x=>x.key).join('\0');
  restoring = true;
  try{
    applyOrder(snap.order);
    const inc = new Set(snap.included);
    for(const it of ITEMS) if(!it.isLogo){ it.included=inc.has(it.key); setCard(it); }
    // Before relayout: the stamp decides the runs, so restoring order without
    // it would undo the move on screen and keep the new pack number underneath.
    if(snap.packs){
      const want=new Map(snap.packs);
      for(const it of ITEMS){
        if(want.has(it.key)) it.pack=want.get(it.key);
        else delete it.pack;  // Absence is part of the snapshot, too.
      }
    }
    holdOrigins.clear();
    for(const [k,v] of snap.holds || []) holdOrigins.set(k,v);
    clearPicked();
    for(const k of snap.picked || []) markPicked(k,true);
    selMode=!!snap.selMode; lastPick=snap.lastPick; lastIdx=snap.lastIdx;
    document.body.classList.toggle('selmode',selMode);
    document.getElementById('selmode').setAttribute('aria-pressed',String(selMode));
    paintSelLabel();
    ANIM_ON=snap.anim; prefs.set('animOn',ANIM_ON?'1':'0');
    setZoom(snap.zoom); applyBg(snap.bg);
    relayout(); updateCount(); applyAnim(); markSelDirty();
    if(persist && before !== ITEMS.filter(x=>!x.isLogo).map(x=>x.key).join('\0')) saveOrder();
    updateHistoryButtons();
  }finally{ restoring=false; }
}
function undo(){
  if(!past.length) return;
  future.push(snapshot()); applySnapshot(past.pop()); toast('Undone');
}
function redo(){
  if(!future.length) return;
  past.push(snapshot()); applySnapshot(future.pop()); toast('Redone');
}
function updateHistoryButtons(){
  document.getElementById('undo').disabled=!past.length;
  document.getElementById('redo').disabled=!future.length;
}
document.getElementById('undo').onclick=undo;
document.getElementById('redo').onclick=redo;
addEventListener('keydown',e=>{
  if(!(e.ctrlKey||e.metaKey) || e.target.closest('input,textarea,[contenteditable="true"]')) return;
  const k=e.key.toLowerCase();
  if(k==='z'&&!e.shiftKey){e.preventDefault();undo();}
  else if(k==='y'||(k==='z'&&e.shiftKey)){e.preventDefault();redo();}
});
document.getElementById('resetAll').onclick=()=>{
  if(!savedSnapshot) return;
  remember(); applySnapshot(savedSnapshot);
  // A previously queued Save must not arrive later and overwrite the reset.
  if(pendingSel !== null || selFlight) queueSelection(snapshot());
  toast('Restored the last Save');
};

function pickAll(){
  remember();
  for(const it of ITEMS) if(!it.isLogo && it.included) markPicked(it.key,true);
  paintSelLabel();
}
function invertPicked(){
  remember();
  for(const it of ITEMS) if(!it.isLogo && it.included) markPicked(it.key,!picked.has(it.key));
  paintSelLabel();
}
const zoomInput=document.getElementById('zoomReset');
function applyZoomInput(){
  const n=Number(zoomInput.value.replace(/%$/,'').trim());
  if(Number.isFinite(n)&&n>0) setZoom(n/100);
  paintZoom(); zoomInput.blur();
}
zoomInput.addEventListener('keydown',e=>{
  if(e.key==='Enter'){e.preventDefault();applyZoomInput();}
  else if(e.key==='Escape'){e.preventDefault();paintZoom();zoomInput.blur();}
});
zoomInput.addEventListener('blur',applyZoomInput);
zoomInput.addEventListener('focus',()=>zoomInput.select());
zoomInput.addEventListener('dblclick',()=>setZoom(1));

const holding=document.getElementById('holding');
const holdCards=document.getElementById('holdCards');
const holdCount=document.getElementById('holdCount');
let holdDragKeys=null;

function assignedPacks(){
  const starts=packStarts().starts, result=new Map();
  let run=0;
  for(let i=0;i<ITEMS.length;i++){
    while(run+1<starts.length&&starts[run+1].index<=i)run++;
    result.set(ITEMS[i].key,ITEMS[i].pack ?? starts[run]?.pack ?? 'new:'+run);
  }
  return result;
}

function originFor(it){
  const index=ITEMS.indexOf(it);
  let pack=it.pack ?? null;
  if(pack===null){
    const starts=packStarts().starts;
    const run=starts.filter(s=>s.index<=index).pop();
    pack=run ? (run.pack ?? 'new:'+starts.indexOf(run)) : 'new:0';
  }
  const starts=packStarts().starts;
  const run=starts.filter(s=>s.index<=index).pop();
  const start=run?run.index:0;
  return {index,pack,slot:ITEMS.slice(start,index).filter(x=>x.included||x.isLogo).length,
          anchor:ITEMS[start]?.key};
}
function setIncluded(it,on){
  if(it.isLogo || it.included===on) return;
  if(!on) holdOrigins.set(it.key,originFor(it));
  else holdOrigins.delete(it.key);
  it.included=on; setCard(it);
}

function renderHolding(){
  const held=ITEMS.filter(x=>!x.isLogo&&!x.included);
  holdCount.textContent=held.length;
  document.getElementById('unholdAll').disabled=!held.length;
  const existing=new Map([...holdCards.querySelectorAll('.hcard')].map(n=>[n.dataset.key,n]));
  const desired=[];
  for(const it of held){
    let c=existing.get(it.key);
    if(!c){
      c=el('div','hcard'); c.draggable=true; c.dataset.key=it.key;
      c.title=it.label||it.key;
      const img=el('img');
      img.alt=it.label||'';img.loading='lazy';img.decoding='async';
      img.src='/preview/'+encodeURIComponent(it.key)+'?still=1&size=72';
      const pick=el('span','pick');
      const button=el('button','','Unhold'); button.type='button'; button.draggable=false;
      button.setAttribute('aria-label','Unhold '+(it.label||it.key));
      // Picked held emoji come back together; an unpicked one is still the
      // single-card gesture that has always been here.
      button.onclick=e=>{
        e.stopPropagation();
        const many=heldPicked();
        unholdKeys(many.includes(it.key)&&many.length>1?many:[it.key]);
      };
      c.append(img,pick,button);
    }
    c.classList.toggle('picked',picked.has(it.key));
    desired.push(c); existing.delete(it.key);
  }
  for(const c of existing.values()){
    const video=c.querySelector('video');
    if(video){if(videoIO)videoIO.unobserve(video);attachVideo(video,false);}
    c.remove();
  }
  // Reuse thumbnails. Rebuilding every image on a pick or undo discarded their decoders.
  let cursor=holdCards.firstChild;
  for(const c of desired){
    if(c===cursor)cursor=cursor.nextSibling;else holdCards.insertBefore(c,cursor);
  }
  while(cursor){const n=cursor;cursor=cursor.nextSibling;n.remove();}
  if(!held.length) holdCards.appendChild(el('span','hempty','Empty'));
  measure(); render();
}
const originalUpdateCount=updateCount;
updateCount=function(){originalUpdateCount();renderHolding();};

// --- The tray joins selection mode ---------------------------------------
// Held emoji were pickable nowhere: the pick box lives on a grid card, and a
// held one has no grid card, so they could only be moved one Unhold at a time.
function heldOrder(){return ITEMS.filter(x=>!x.isLogo&&!x.included).map(x=>x.key);}
function heldPicked(){return heldOrder().filter(k=>picked.has(k));}
// markPicked paints cards.get(key) -- the GRID node, which a held emoji lacks.
const originalMarkPicked=markPicked;
markPicked=function(key,on){
  originalMarkPicked(key,on);
  for(const c of holdCards.children)
    if(c.dataset&&c.dataset.key===key)c.classList.toggle('picked',on);
};
// CLICK, on the whole card -- and click is what makes this safe rather than a
// gesture conflict. A drag emits dragstart/drop/dragend and NO click, measured,
// so the two gestures separate themselves by what the user DID. The previous
// version separated them by where the user PRESSED: a pointerdown on the pick
// box only. That box is 1.3em on a 64px card, about 17px, so selecting a run
// meant hitting a small target twice and a click on the card did nothing --
// which is why multi-select, and the multi-card drag that depends on it, both
// looked broken while the logic underneath was correct.
//
// One handler, not two. Keeping the box's own pointerdown as well would make a
// click on the box toggle twice and net to zero. The box is a child of the
// card, so this covers it; the tray has no drag-paint, and `dragstart` already
// refuses a drag that begins on the box.
let lastHeldPick=null;
holdCards.addEventListener('click',e=>{
  if(!selMode)return;
  // Unhold owns its own click and must never move the selection.
  if(e.target.closest('button'))return;
  const card=e.target.closest('.hcard'); if(!card)return;
  const order=heldOrder(), i=order.indexOf(card.dataset.key);
  if(i<0)return;
  const anchor=order.indexOf(lastHeldPick);
  const on=!picked.has(order[i]);
  const [a,b]=e.shiftKey&&anchor>=0?[Math.min(anchor,i),Math.max(anchor,i)]:[i,i];
  for(let k=a;k<=b;k++)markPicked(order[k],on);
  if(!e.shiftKey||anchor<0)lastHeldPick=order[i];
  paintSelLabel();
});

function canUnhold(keys,atDrop=false){
  const counts=new Map(), assignments=assignedPacks();
  for(const it of ITEMS){
    if(!it.included && !it.isLogo)continue;
    const pack=assignments.get(it.key);
    counts.set(pack,(counts.get(pack)||0)+1);
  }
  for(const key of keys){
    const it=ITEMS.find(x=>x.key===key);
    if(!it||it.isLogo||it.included)continue;
    // At a DROP the destination is the run the card was dragged into, which is
    // the whole point of the gesture. Reading `assignments` here instead asked
    // whether the emoji's OWN pack had room -- so moving one out of a full pack
    // into a roomy one was refused, and moving into a full one was allowed.
    // The aimed pack answers it now, for the same reason the stamp uses it: at
    // a boundary the resting place names the pack above, not the one aimed at.
    // Only when NOTHING was aimed at does capacity fall back to the resting
    // place -- the guard must keep asking, and `undefined` is tested rather
    // than `??` because `null` is a real answer (a run with no number yet).
    const destination=atDrop
      ? (aimedPack!==undefined?aimedPack:runPackAt(ITEMS.indexOf(it),new Set(keys)))
      : null;
    const origin=atDrop?{pack:destination??assignments.get(key)}
                       :holdOrigins.get(key)||{pack:assignments.get(key)};
    const count=(counts.get(origin.pack)||0)+1;
    if(count>PER_SET){
      const label=typeof origin.pack==='number'?'Pack '+origin.pack:'That pack';
      toast(atDrop
        ? `${label} is full (${PER_SET}/${PER_SET}). Hold one of its emoji first, then bring this one in.`
        : `${label} is full (${PER_SET}/${PER_SET}). Its original place is occupied; hold or move another emoji before Unhold.`);
      return false;
    }
    counts.set(origin.pack,count);
  }
  return true;
}
function unholdKeys(keys){
  keys=keys.filter(k=>ITEMS.some(x=>x.key===k&&!x.isLogo&&!x.included));
  if(!keys.length||!canUnhold(keys))return false;
  remember();
  const restoringItems=keys.map(k=>ITEMS.find(x=>x.key===k))
    .map(it=>({it,origin:holdOrigins.get(it.key)||originFor(it)}))
    .sort((a,b)=>a.origin.index-b.origin.index);
  for(const {it,origin} of restoringItems){
    ITEMS.splice(ITEMS.indexOf(it),1);
    const assignments=assignedPacks();
    const samePack=ITEMS.filter(x=>(x.included||x.isLogo)&&assignments.get(x.key)===origin.pack);
    const next=samePack[origin.slot];
    const previous=samePack[samePack.length-1];
    const anchor=ITEMS.findIndex(x=>x.key===origin.anchor);
    const at=next?ITEMS.indexOf(next):previous?ITEMS.indexOf(previous)+1
      :anchor>=0?anchor+1:Math.min(origin.index,ITEMS.length);
    ITEMS.splice(at,0,it);setIncluded(it,true);
  }
  relayout();updateCount();markSelDirty();saveOrder();return true;
}
document.getElementById('unholdAll').onclick=()=>unholdKeys(ITEMS.filter(x=>!x.included&&!x.isLogo).map(x=>x.key));

function holdKeys(keys,snap){
  const items=ITEMS.filter(x=>keys.has(x.key)&&!x.isLogo&&x.included);
  if(!items.length)return;
  remember(snap);
  const origins=new Map(items.map(it=>[it.key,originFor(it)]));
  for(const it of items){setIncluded(it,false);holdOrigins.set(it.key,origins.get(it.key));}
  clearPicked();relayout();updateCount();markSelDirty();
}
document.getElementById('toHold').onclick=()=>{
  if(!picked.size){toast('Pick something first');return;}
  holdKeys(picked);
};
holding.addEventListener('dragover',e=>{
  if(dragKey===null||holdDragKeys)return;
  e.preventDefault();e.dataTransfer.dropEffect='move';holding.classList.add('drop');
});
holding.addEventListener('dragleave',e=>{
  if(!holding.contains(e.relatedTarget))holding.classList.remove('drop');
});
holding.addEventListener('drop',e=>{
  holding.classList.remove('drop');
  if(dragKey===null||holdDragKeys)return;
  e.preventDefault();e.stopPropagation();
  const snap=dragSnap, keys=new Set(carried);
  applyOrder(snap.order);endDrag(true);holdKeys(keys,snap);
});
holdCards.addEventListener('dragstart',e=>{
  const card=e.target.closest('.hcard');
  if(!card||e.target.closest('button')||e.target.closest('.pick')){e.preventDefault();return;}
  dragKey=card.dataset.key;dragSnap=snapshot();aimedPack=undefined;
  // Dragging a PICKED held card carries every picked held emoji, the same way
  // the grid carries a picked run; an unpicked one stays the single gesture.
  const many=heldPicked();
  holdDragKeys=new Set(many.includes(dragKey)&&many.length>1?many:[dragKey]);
  carried.clear();for(const k of holdDragKeys)carried.add(k);
  e.dataTransfer.effectAllowed='move';
  try{e.dataTransfer.setData('text/plain',dragKey);}catch(_){}
});
// The actual grid drop commits inclusion BEFORE recording history. dropEffect
// at dragend is not evidence of a committed drop and previously lost the undo.
/** The pack of the run a card at `index` has landed in.
 *
 *  `ignore` is the set being dragged, and leaving it out is a real bug, not a
 *  refinement: a dropped card still carries the pack it came FROM, so
 *  `packStarts()` cuts a fresh one-card run at it and any "which run covers
 *  this index" answer is that island -- the card's own old pack. Reading the
 *  nearest settled neighbour instead gives the pack the owner actually dropped
 *  it into. Backwards first (a drop belongs to the run above it), then forwards
 *  for a drop above every live card. */
function runPackAt(index,ignore){
  const settled=i=>{
    const it=ITEMS[i];
    if(!it||ignore&&ignore.has(it.key))return null;
    if(!it.included&&!it.isLogo)return null;
    return it.pack??null;
  };
  for(let i=index-1;i>=0;i--){const p=settled(i);if(p!=null)return p;}
  for(let i=index+1;i<ITEMS.length;i++){const p=settled(i);if(p!=null)return p;}
  return null;
}
/** Accept a drop on the grid: every drop, from the tray or from the grid.
 *
 *  THE PANEL STATES THE INTENDED LAYOUT. It is not a mirror of what is live on
 *  Telegram: the owner drags an emoji into the pack they want it to end up in,
 *  saves, and the publish step performs the real move afterwards. So a
 *  cross-pack drop is accepted -- and the emoji must be RE-STAMPED into the
 *  destination pack. Without the stamp `packStarts()` re-groups it by its old
 *  pack number, it becomes a one-card run still labelled with the pack it came
 *  from, and the drag reads as having snapped back.
 *
 *  ONE path, for both sources. This used to return early for anything that was
 *  not a held card, so a grid-to-grid drag never re-stamped: an emoji carried
 *  from pack 1 into the middle of pack 4 kept pack 1 and CUT PACK 4 IN TWO.
 *  Capacity is still the only thing a drop refuses -- that is what the holding
 *  tray is for: park one, then bring the replacement in. */
function acceptDrop(){
  const keys=[...(holdDragKeys||carried)];
  if(holdDragKeys&&!canUnhold(keys,true))return false;
  for(const k of keys){
    const it=ITEMS.find(x=>x.key===k);
    if(!it||it.isLogo)continue;
    // A number is the destination; `null` is a run with no number yet, and the
    // emoji must join it by LOSING its own -- carrying an old number there is
    // what cut a false run inside a pack that has not been published. Nothing
    // aimed at, nothing stamped.
    if(typeof aimedPack==='number'){ if(it.pack!==aimedPack)it.pack=aimedPack; }
    else if(aimedPack===null&&it.pack!=null) delete it.pack;
  }
  if(holdDragKeys){
    for(const k of holdDragKeys)setIncluded(ITEMS.find(x=>x.key===k),true);
    holdDragKeys=null;
  }
  relayout();updateCount();markSelDirty();return true;
}
holdCards.addEventListener('dragend',()=>{holdDragKeys=null;});

function exportDraft(){
  const blob=new Blob([JSON.stringify({version:1,snapshot:snapshot()},null,2)],{type:'application/json'});
  const url=URL.createObjectURL(blob), a=el('a');
  a.href=url;a.download='numera-emoji-mapper-draft.json';a.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
}
for(const it of ITEMS)if(!it.included&&!it.isLogo)holdOrigins.set(it.key,originFor(it));
renderHolding();
savedSnapshot=snapshot();


// Logging is separate from the save queues: a log failure cannot block curation.
const uiEvents=[];
let uiLogTimer=null, uiLogFlight=false;
function logUI(event,fields={}){
  if(uiEvents.length>=32)uiEvents.shift();
  uiEvents.push({event,...fields});
  if(uiLogTimer===null)uiLogTimer=setTimeout(flushUILog,1000);
}
async function flushUILog(){
  uiLogTimer=null;
  if(uiLogFlight||!uiEvents.length)return;
  uiLogFlight=true;
  const ac=new AbortController(), deadline=setTimeout(()=>ac.abort(),3000);
  const batch=uiEvents.splice(0,32);
  try{
    await fetch('/api/client-log',{method:'POST',headers:{'Content-Type':'application/json','X-Panel-Token':TOK},
      body:JSON.stringify({events:batch}),signal:ac.signal,keepalive:true});
  }catch(_){ /* best effort, without an infinite retry queue */ }
  finally{clearTimeout(deadline);uiLogFlight=false;}
}
const loggedButtons={toHold:'hold',unholdAll:'unhold',undo:'undo',redo:'redo',resetAll:'reset',
  selmode:'selection',all:'selection',none:'selection',inv:'selection',anim:'animation',
  bg:'backdrop',zoomIn:'zoom',zoomOut:'zoom',zoomReset:'zoom'};
document.addEventListener('click',e=>{
  const button=e.target.closest('button');
  const name=button&&(loggedButtons[button.id]||(button.closest('.hcard')?'unhold':null));
  if(name)logUI(name,{count:picked.size,zoom});
});
addEventListener('error',e=>{
  const source=String(e.filename||'').split('/').pop().split('?')[0];
  const known=['panel-grid.js','panel-actions.js','panel-holding.js'];
  const name=e.error&&/^[A-Za-z]{0,30}Error$/.test(e.error.name)?e.error.name:'Error';
  logUI('error',{name,source:known.includes(source)?source:'window',line:e.lineno||0,column:e.colno||0});
});
addEventListener('unhandledrejection',e=>{
  const name=e.reason&&/^[A-Za-z]{0,30}Error$/.test(e.reason.name)?e.reason.name:'Error';
  logUI('error',{name,source:'promise'});
});
logUI('ready',{count:ITEMS.length,zoom});
