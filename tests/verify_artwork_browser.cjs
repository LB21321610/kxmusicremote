/* One isolated Chrome session, using the existing loopback fixture and CDP
   pattern. No requests to Apple, playback commands, or user settings writes.
   Image retries use real time; the 65 s catalog timer is advanced explicitly. */
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {spawn}=require('node:child_process');
const root=path.resolve(__dirname,'..'),base='http://127.0.0.1:18769';
const workspace=fs.mkdtempSync(path.join(os.tmpdir(),'artwork-browser-'));
const results=[],exceptions=[],images=new Map(),resolves=new Map();
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
let server,chrome,socket,cdp,evaluate,until;
function check(label,details){results.push({check:label,passed:true,...(details?{details}:{})});console.log(label)}
async function fixture(body){await fetch(base+'/__test',{method:'POST',body:JSON.stringify(body)})}
async function song(title,loaded=true){
  await fixture({state:{ok:true,loaded,title,artist:'Catalog Artist',lyric_artist:'Catalog Artist',album:'Catalog Album',
    dur:180,pos:10,playing:true}});
  await evaluate('poll()');await until('!pollBusy');await evaluate('poll()');
}
function svg(color,size=600){return `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}"><rect width="100%" height="100%" fill="${color}"/></svg>`}
async function intercept(event){
  const u=new URL(event.request.url),id=event.requestId;
  const reply=(body,type='application/json',code=200)=>cdp('Fetch.fulfillRequest',{requestId:id,responseCode:code,
    responseHeaders:[{name:'Content-Type',value:type},{name:'Cache-Control',value:'public, max-age=3600'}],
    body:Buffer.from(body).toString('base64')}).catch(()=>{});
  if(u.pathname==='/artwork'){
    const title=JSON.parse(u.searchParams.get('track')).title,token=u.searchParams.get('request');
    if(!images.has(title))images.set(title,new Map());
    const attempts=images.get(title);
    if(!attempts.has(token))attempts.set(token,{at:Date.now(),url:u.href});
    const n=[...attempts.keys()].indexOf(token)+1;
    if(title==='Failure'&&(n===1||n===3))return reply('','image/jpeg',404);
    if(title==='Always fails')return reply('','image/jpeg',404);
    if(title==='Old image')await sleep(2200);
    return reply(svg(title==='Placeholder'&&n===1?'#777':n===4?'#3080d0':'#d04060',
      title==='Placeholder'&&n===1?8:600),'image/svg+xml');
  }
  if(u.pathname==='/catalog/resolve'){
    const title=u.searchParams.get('title'),n=(resolves.get(title)||0)+1;resolves.set(title,n);
    if(title==='Old resolve')await sleep(2200);
    if(title==='Retry'&&n===1)return reply(JSON.stringify({ok:false,error:'unavailable'}),'application/json',503);
    if(['Video','Retry','Old resolve'].includes(title))return reply(JSON.stringify({ok:true,match:null,
      reason:'ambiguous',artwork_album_id:'1895174191'}));
    if(title==='Legacy')return reply(JSON.stringify({ok:true,reason:null,match:{id:'2001',album_id:'1001'}}));
    return reply(JSON.stringify({ok:true,match:null,reason:'not_found',artwork_album_id:null}));
  }
  if(u.pathname==='/catalog/album/1895174191')return reply(JSON.stringify({ok:true,id:'1895174191',artists:[],tracks:[],
    artwork:null,motion:{square:{hls:'/__motion/artwork.m3u8',file:{url:'/__motion/artwork.mp4'}}}}));
  await cdp('Fetch.continueRequest',{requestId:id});
}
async function start(){
  server=spawn(path.join(root,'.venv/Scripts/python.exe'),[path.join(__dirname,'lyrics_fixture_server.py')],
    {cwd:workspace,windowsHide:true,stdio:'ignore',env:{...process.env,REMOTE_FIXTURE_PORT:'18769',
      REMOTE_FIXTURE_SETTINGS:path.join(workspace,'settings.json')}});
  let ready=false;
  for(let i=0;i<100;i++){try{if((await fetch(base+'/settings/lyrics')).ok){ready=true;break}}catch{}await sleep(100)}
  assert.ok(ready,'fixture started');
  const profile=path.join(workspace,'chrome');
  chrome=spawn('C:/Program Files/Google/Chrome/Application/chrome.exe',['--headless=new','--no-first-run',
    '--no-default-browser-check','--autoplay-policy=no-user-gesture-required','--window-size=1440,1000',
    '--disable-background-timer-throttling','--disable-renderer-backgrounding','--remote-debugging-port=0',
    '--user-data-dir='+profile,'about:blank'],{windowsHide:true,stdio:'ignore'});
  for(let i=0;i<100&&!fs.existsSync(path.join(profile,'DevToolsActivePort'));i++)await sleep(100);
  const port=fs.readFileSync(path.join(profile,'DevToolsActivePort'),'utf8').split('\n')[0];
  const target=(await(await fetch('http://127.0.0.1:'+port+'/json/list')).json()).find(t=>t.type==='page');
  socket=new WebSocket(target.webSocketDebuggerUrl);const pending=new Map();let seq=0;
  await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
  cdp=(method,params={})=>new Promise((resolve,reject)=>{const id=++seq;
    const timer=setTimeout(()=>{pending.delete(id);reject(Error('CDP timeout '+method))},15000);
    pending.set(id,{resolve,reject,timer});socket.send(JSON.stringify({id,method,params}))});
  socket.onmessage=event=>{const msg=JSON.parse(event.data);
    if(msg.method==='Runtime.exceptionThrown')exceptions.push(msg.params.exceptionDetails);
    if(msg.method==='Fetch.requestPaused')intercept(msg.params).catch(e=>exceptions.push(String(e)));
    if(msg.id){const p=pending.get(msg.id);if(p){pending.delete(msg.id);clearTimeout(p.timer);
      msg.error?p.reject(Error(JSON.stringify(msg.error))):p.resolve(msg.result)}}};
  evaluate=async expression=>{const r=await cdp('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});
    if(r.exceptionDetails)throw Error(r.exceptionDetails.exception?.description||r.exceptionDetails.text);return r.result.value};
  until=async(expression,timeout=10000)=>{const end=Date.now()+timeout;
    while(Date.now()<end){if(await evaluate(expression))return;await sleep(40)}throw Error('Timed out: '+expression)};
  await cdp('Runtime.enable');await cdp('Page.enable');
  await cdp('Emulation.setFocusEmulationEnabled',{enabled:true});
  await cdp('Fetch.enable',{patterns:[{urlPattern:'*/artwork?*'},{urlPattern:'*/catalog/*'}]});
  await cdp('Page.addScriptToEvaluateOnNewDocument',{source:`
    window.__retryTimers=new Map();const nativeSetTimeout=window.setTimeout;
    window.setTimeout=function(fn,ms,...args){const id=nativeSetTimeout(fn,ms,...args);
      if(ms===65000)__retryTimers.set(id,{fn:()=>fn(...args),ms});return id};
    window.__advanceRetry=id=>{const timer=__retryTimers.get(id);clearTimeout(id);__retryTimers.delete(id);timer.fn()};
    window.__swaps=0;new MutationObserver(records=>{for(const r of records)if(r.type==='attributes'&&r.attributeName==='class'&&
      r.target.id==='art'&&r.target.classList.contains('art-swap')&&!String(r.oldValue).includes('art-swap'))++__swaps
    }).observe(document,{subtree:true,attributes:true,attributeOldValue:true,attributeFilter:['class']});`});
  await fixture({state:{ok:true,loaded:true,title:'Failure',artist:'Catalog Artist',lyric_artist:'Catalog Artist',
    album:'Catalog Album',dur:180,pos:10,playing:true}});
  await cdp('Page.navigate',{url:base});
  await until("typeof artworkJob!=='undefined'&&!!artworkJob");
}
async function run(){
  await until("$('art').classList.contains('art-missing')");
  await until("$('art').naturalWidth===600&&!!BG.to");
  const first=await evaluate("$('art').src"),swaps=await evaluate('__swaps');
  await until('artToken>'+new URL(first).searchParams.get('request'));await sleep(150);
  assert.equal(await evaluate("$('art').src"),first,'failed supplemental request retains cover');
  await until("$('art').src!=="+JSON.stringify(first)+"&&$('art').naturalWidth===600",8000);
  assert.equal(await evaluate('__swaps'),swaps,'supplements do not replay entrance animation');
  await sleep(350);
  const times=[...images.get('Failure').values()].map(x=>x.at);
  assert.equal(times.length,4);
  const offsets=times.map(t=>t-times[0]);
  offsets.forEach((actual,i)=>assert.ok(Math.abs(actual-[0,1000,3000,8000][i])<650,JSON.stringify(offsets)));
  assert.equal(new Set([...images.get('Failure').values()].map(x=>x.url)).size,4);
  check('Initial image failure recovers; retries at 1/3/8 s stop after four unique requests; later failure keeps cover; no repeated entrance',{offsets});

  await song('Placeholder');await until("$('art').naturalWidth===8");
  await until("$('art').naturalWidth===600&&!!BG.to");
  check('A successfully decoded temporary placeholder is replaced automatically');

  await song('Old image');await until('artworkJob.images.size>0');
  await evaluate('window.__oldJob=artworkJob;window.__lateImage=[...artworkJob.images][0].onload');
  await song('New image');await until("$('art').naturalWidth===600");
  await evaluate('__lateImage()');await sleep(2300);
  assert.equal(await evaluate("$('art').src.includes('New%20image')||decodeURIComponent($('art').src).includes('New image')"),true);
  assert.equal(images.get('Old image').size,1,'old 1 s timer cancelled');
  check('Track changes cancel old image requests/timers and reject an already queued old callback');

  await song('Always fails');await until("$('art').classList.contains('art-missing')");
  check('A track without any successful image uses the placeholder');
  await song('Old image');await until('artworkJob.images.size>0');
  await song('',false);await until("!artworkJob&&nowCatalog.key===''&&!$('art').hasAttribute('src')");
  await sleep(1100);
  check('Unloading the song cancels artwork and catalog work');

  await song('Old resolve');await until("nowCatalog.state==='pending'&&!!nowCatalog.controller");
  await sleep(600);await evaluate('window.__oldCatalog=nowCatalog');
  await song('New resolve');await until("nowCatalog.state==='none'");await sleep(2300);
  assert.equal(await evaluate("__oldCatalog.controller.signal.aborted&&nowCatalog.album===null"),true);
  check('Late catalog responses cannot attach artwork to a new track');

  await song('Video');await until("nowCatalog.album&&nowCatalog.album.id==='1895174191'");
  assert.equal(await evaluate("nowCatalog.match===null&&nowCatalog.state==='none'&&!document.querySelector('.track.current')"),true);
  await until("motionFailed.has('/__motion/artwork.mp4')");
  assert.ok(await evaluate("!$('art-motion').classList.contains('on')&&!document.body.classList.contains('motion-square')&&$('art').naturalWidth===600&&!!BG.to"));
  check('artwork_album_id starts motion loading while match stays null; real missing HLS/MP4 fall back to the static cover');
  // Model successful playback only to exercise the existing display/pause/switch rules.
  await evaluate(`window.__nativePlay=HTMLMediaElement.prototype.play;window.__nativePause=HTMLMediaElement.prototype.pause;
    window.__ignoreMediaError=e=>e.stopImmediatePropagation();
    for(const id of ['art-motion','bg-motion'])$(id).addEventListener('error',__ignoreMediaError,true);
    HTMLMediaElement.prototype.play=function(){const v=this;Object.defineProperty(v,'paused',{configurable:true,get:()=>false});
      Object.defineProperty(v,'readyState',{configurable:true,get:()=>4});
      setTimeout(()=>v.dispatchEvent(new Event('playing')),0);return Promise.resolve()};
    HTMLMediaElement.prototype.pause=function(){Object.defineProperty(this,'paused',{configurable:true,get:()=>true})};
    motionFailed.clear();motionUpdate()`);
  await until("$('art-motion').classList.contains('on')");
  await evaluate("playing=false;motionUpdate()");
  assert.ok(await evaluate("!$('art-motion').classList.contains('on')&&$('art').naturalWidth===600"));
  await evaluate("playing=true;motionEnabled=false;motionUpdate()");
  assert.ok(await evaluate("!motionTarget().want"));
  await cdp('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]});
  await evaluate('motionEnabled=true;motionUpdate()');assert.ok(await evaluate('!motionTarget().want'));
  await cdp('Emulation.setEmulatedMedia',{features:[]});
  await evaluate(`HTMLMediaElement.prototype.play=__nativePlay;HTMLMediaElement.prototype.pause=__nativePause;
    for(const id of ['art-motion','bg-motion']){$(id).removeEventListener('error',__ignoreMediaError,true);delete $(id).paused;delete $(id).readyState}`);
  check('Motion display, pause, switch and reduced-motion rules remain effective (successful media playback modelled)');

  await song('Legacy');await until("nowCatalog.match&&nowCatalog.album&&nowCatalog.album.id==='1001'");
  check('An older resolve response without artwork_album_id still uses match.album_id');

  await song('Retry');await until("nowCatalog.state==='error'&&!!nowCatalog.retryTimer");
  assert.equal(await evaluate('__retryTimers.get(nowCatalog.retryTimer).ms'),65000);
  await evaluate('__advanceRetry(nowCatalog.retryTimer)');await until("nowCatalog.album&&nowCatalog.album.id==='1895174191'");
  await sleep(200);assert.equal(resolves.get('Retry'),2);
  assert.equal(await evaluate('__retryTimers.has(nowCatalog.retryTimer)'),false);
  check('A temporary resolve failure gets exactly one additional attempt after the 65 s timer (clock advanced)');
  await song('Cancel retry');await until('!!nowCatalog.retryTimer');
  await evaluate('window.__cancelled=__retryTimers.get(nowCatalog.retryTimer).fn');
  await song('Final song');await evaluate('__cancelled()');await sleep(650);
  assert.equal(resolves.get('Cancel retry'),1);
  assert.deepEqual(exceptions,[]);
  check('Track change cancels the delayed match attempt; no JavaScript exceptions');
}
(async()=>{let failure;
  try{await start();await run()}catch(e){failure=e;throw e}finally{
    if(cdp)try{await cdp('Browser.close')}catch{}if(socket)socket.close();
    if(chrome)chrome.kill();if(server){const stopped=new Promise(r=>server.once('exit',r));server.kill();await stopped}
    fs.writeFileSync(path.join(__dirname,'artifacts/artwork-recovery-browser.json'),JSON.stringify({
      passed:!failure,method:'One isolated headless Windows Chrome; existing local fixture; actual image/media failures; successful video display modelled; 65 s retry timer advanced explicitly; no Apple Music commands or external catalog traffic.',
      results,exceptions,error:failure?.stack},null,2));
  }
})().catch(e=>{console.error(e);process.exitCode=1});
