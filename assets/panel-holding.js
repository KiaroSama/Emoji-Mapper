// Curation history, holding and explicit Save checkpoints share one model.
'use strict';

const HISTORY_MAX = 100;
let past = [], future = [], restoring = false;
const holdOrigins = new Map();
let savedSnapshot = null, pendingSaveSnapshot = null;

function snapshot(){
  return {order: ITEMS.map(x=>x.key), included: ITEMS.filter(x=>x.included).map(x=>x.key),
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
      const pick=el('span','pick','✓');
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
// Pointer events, not click: the card's own draggable owns dragging, and one
// element cannot run both gestures. Starting on the box is what tells them apart.
let lastHeldPick=null;
holdCards.addEventListener('pointerdown',e=>{
  if(!selMode)return;
  const box=e.target.closest('.pick'); if(!box)return;
  const card=box.closest('.hcard'); if(!card)return;
  e.preventDefault();e.stopPropagation();
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
    const origin=atDrop?{pack:assignments.get(key)}:holdOrigins.get(key)||{pack:assignments.get(key)};
    const count=(counts.get(origin.pack)||0)+1;
    if(count>PER_SET){
      const label=typeof origin.pack==='number'?'Pack '+origin.pack:'The original pack';
      toast(`${label} is full (${PER_SET}/${PER_SET}). Its original place is occupied; hold or move another emoji before Unhold.`);
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
  dragKey=card.dataset.key;dragSnap=snapshot();
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
/** The pack whose run covers this array index, ignoring the item's own field. */
function runPackAt(index){
  const run=packStarts().starts.filter(s=>s.index<=index).pop();
  return run?run.pack:null;
}
function acceptHeldDrop(){
  if(!holdDragKeys)return true;
  const keys=[...holdDragKeys];
  // A PUBLISHED emoji cannot change packs by being dragged. Telegram has no
  // move-between-sets call: it would be delete + re-add, which mints a NEW
  // custom_emoji_id and breaks every stored reference to the old one. The drop
  // used to be accepted and then re-grouped by the item's own pack field, so
  // the card snapped back with no explanation and read as a broken drag.
  // Only a POSITIVELY identified different pack refuses -- an undetermined
  // run must never block a working same-pack move.
  for(const k of keys){
    const it=ITEMS.find(x=>x.key===k);
    if(!it||it.pack==null)continue;
    const target=runPackAt(ITEMS.indexOf(it));
    if(target!=null&&target!==it.pack){
      toast(`Pack ${it.pack} is live on Telegram, so this emoji cannot move to pack ${target} `
            +`(that would re-add it under a new id). Unhold puts it back in pack ${it.pack}.`);
      return false;
    }
  }
  if(!canUnhold(keys,true))return false;
  for(const k of holdDragKeys)setIncluded(ITEMS.find(x=>x.key===k),true);
  holdDragKeys=null;relayout();updateCount();markSelDirty();return true;
}
holdCards.addEventListener('dragend',()=>{holdDragKeys=null;});

function exportDraft(){
  const blob=new Blob([JSON.stringify({version:1,snapshot:snapshot()},null,2)],{type:'application/json'});
  const url=URL.createObjectURL(blob), a=el('a');
  a.href=url;a.download='emoji-mapper-draft.json';a.click();
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
