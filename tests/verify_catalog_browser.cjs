/* Catalog search, album/artist pages, playback hand-off and the new backdrop.
   Real headless Edge and Chrome via CDP against the loopback fixture; the
   fixture never touches Apple Music, the network or system audio. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const {spawn}=require('node:child_process');
const root=path.resolve(__dirname,'..'),base='http://127.0.0.1:18768';
const output=path.join(__dirname,'artifacts');
const workspace=fs.mkdtempSync(path.join(os.tmpdir(),'catalog-browser-'));
const results=[];
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
let server;
async function startServer(){
  server=spawn(path.join(root,'.venv/Scripts/python.exe'),[path.join(__dirname,'lyrics_fixture_server.py')],
    {cwd:workspace,windowsHide:true,stdio:'ignore',
     env:{...process.env,REMOTE_FIXTURE_PORT:'18768',REMOTE_FIXTURE_SETTINGS:path.join(workspace,'settings.json')}});
  for(let i=0;i<100;i++){
    try{if((await fetch(base+'/settings/lyrics')).ok)return}catch{}
    await sleep(100);
  }
  throw Error('Fixture failed to start');
}
async function stopServer(){
  if(!server)return;const stopped=new Promise(r=>server.once('exit',r));server.kill();await stopped;server=null;
}
async function fixture(body){await fetch(base+'/__test',{method:'POST',body:JSON.stringify(body)})}
async function calls(){return (await(await fetch(base+'/__test')).json()).calls}
const CATALOG_SONG={ok:true,title:'Catalog Song',artist:'Catalog Artist — Catalog Album',lyric_artist:'Catalog Artist',
  album:'Catalog Album',dur:180,pos:10,playing:true};
async function browser(executable,name,software=false){
  const profile=path.join(workspace,name);
  // GPU on: the page refuses software-only WebGL (failIfMajorPerformanceCaveat); see the software check below.
  const proc=spawn(executable,['--headless=new',...(software?['--disable-gpu']:[]),'--no-first-run','--no-default-browser-check',
    '--disable-background-timer-throttling','--disable-renderer-backgrounding','--disable-backgrounding-occluded-windows',
    '--window-size=1440,1000','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],{windowsHide:true,stdio:'ignore'});
  for(let i=0;i<100&&!fs.existsSync(path.join(profile,'DevToolsActivePort'));i++)await sleep(100);
  const port=fs.readFileSync(path.join(profile,'DevToolsActivePort'),'utf8').split('\n')[0];
  const target=(await(await fetch('http://127.0.0.1:'+port+'/json/list')).json()).find(t=>t.type==='page');
  const socket=new WebSocket(target.webSocketDebuggerUrl),pending=new Map(),exceptions=[];let id=0;
  await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
  socket.onmessage=event=>{
    const msg=JSON.parse(event.data);
    if(msg.method==='Runtime.exceptionThrown')exceptions.push(msg.params.exceptionDetails.text+' '+(msg.params.exceptionDetails.exception?.description||''));
    if(msg.id){const entry=pending.get(msg.id);pending.delete(msg.id);if(entry){clearTimeout(entry.timer);
      msg.error?entry.reject(Error(JSON.stringify(msg.error))):entry.resolve(msg.result)}}
  };
  function cdp(method,params={}){return new Promise((resolve,reject)=>{
    const request=++id,timer=setTimeout(()=>{pending.delete(request);reject(Error('CDP timeout '+method))},20000);
    pending.set(request,{resolve,reject,timer});socket.send(JSON.stringify({id:request,method,params}));
  })}
  async function evaluate(expression){const result=await cdp('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});
    if(result.exceptionDetails)throw Error(result.exceptionDetails.exception?.description||result.exceptionDetails.text);
    return result.result.value}
  async function until(expression,timeout=8000){const end=Date.now()+timeout;
    while(Date.now()<end){if(await evaluate(expression))return;await sleep(60)}
    throw Error('Timed out: '+expression+' '+JSON.stringify(await evaluate("({view,nav:navStack.length,state:nowCatalog.state,error:$('control-error').textContent})")))}
  async function key(keyName,code,vk,modifiers=0){
    await cdp('Input.dispatchKeyEvent',{type:'keyDown',key:keyName,code,windowsVirtualKeyCode:vk,modifiers,
      ...(keyName==='Enter'?{text:'\r',unmodifiedText:'\r'}:{})});
    await cdp('Input.dispatchKeyEvent',{type:'keyUp',key:keyName,code,windowsVirtualKeyCode:vk,modifiers});
  }
  // Let view cross-fades (0.4-0.6 s) finish so screenshots show one page.
  async function shot(file){await sleep(700);const data=await cdp('Page.captureScreenshot',{format:'png'});
    fs.writeFileSync(path.join(output,file),Buffer.from(data.data,'base64'))}
  async function viewport(width,height,mobile){await cdp('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:mobile?2:1,mobile});
    await sleep(250)}
  await cdp('Runtime.enable');await cdp('Page.enable');
  await cdp('Emulation.setFocusEmulationEnabled',{enabled:true});
  return {name,cdp,evaluate,until,key,shot,viewport,exceptions,
    async close(){try{await cdp('Browser.close')}catch{}socket.close();proc.kill()}};
}
function check(name,label,details){results.push({browser:name,check:label,passed:true,...(details?{details}:{})});console.log(name+': '+label)}
const RANGE="(()=>{const s=backdropSample();let lo=765,hi=0;for(let i=0;i<s.data.length;i+=4){const v=s.data[i]+s.data[i+1]+s.data[i+2];"+
  "lo=Math.min(lo,v);hi=Math.max(hi,v)}return {mode:s.mode,width:s.width,height:s.height,range:hi-lo}})()";
// Also rejects stringified values leaking into page text.
const NO_OVERFLOW="(()=>{const s=pageScroller();return document.documentElement.scrollWidth<=innerWidth&&s.scrollWidth<=s.clientWidth+1"+
  "&&!/\\[object |\\bnull\\b|\\bundefined\\b/.test(s.textContent)})()";

async function run(name,executable){
  const b=await browser(executable,name);const {evaluate,until,key,shot,viewport,cdp}=b;
  try{
    await fixture({state:CATALOG_SONG,catalog_search_error:'',catalog_play_error:'',catalog_delay:0,command_error:'',clear_calls:true});
    await viewport(1440,900,false);
    await cdp('Page.navigate',{url:base});
    await until("typeof poll==='function'&&nowCatalog.state==='matched'&&!!nowCatalog.album");
    check(name,'browser version',await cdp('Browser.getVersion'));

    // Backdrop: WebGL draws real pixels at the viewport aspect; theme-color uses the album color.
    const gl=await evaluate(RANGE);
    assert.equal(gl.mode,'webgl');assert.ok(gl.range>10&&gl.width>gl.height,JSON.stringify(gl));
    assert.equal(await evaluate("document.querySelector('meta[name=theme-color]').content"),'rgb(23,38,55)');
    assert.ok(await evaluate("$('bg-canvas').hidden&&!$('bg-gl').hidden"));
    check(name,'WebGL fluid backdrop draws content; theme-color follows the album color',gl);
    const frames=await evaluate("(async()=>{let n=0;const draw=drawFluid;drawFluid=function(){n++;return draw.apply(this,arguments)};"+
      "await new Promise(r=>setTimeout(r,1000));drawFluid=draw;return n})()");
    assert.ok(frames>=8&&frames<=20,'frames per second '+frames);
    check(name,'backdrop renders at about 15 frames per second while playing',{frames});

    // Motion artwork: the fixture's videos are missing. HLS (Chrome/Edge claim support) fails
    // first, then the server-picked MP4 is tried, then the page settles on the backdrop.
    await until("motionFailed.has('/__motion/square.m3u8')&&motionFailed.has('/__motion/square.mp4')",12000);
    assert.ok(await evaluate("!$('art-motion').classList.contains('on')&&!document.body.classList.contains('motion-square')&&!$('art').style.visibility"));
    assert.ok(await evaluate("BG.frame!==0"));
    check(name,'failed HLS motion artwork falls back to the MP4, then to the fluid backdrop');
    // Successful playback modelled by stubbing the media element (no codec in the fixture).
    await evaluate(`(()=>{const P=HTMLMediaElement.prototype;window.__media={play:P.play,pause:P.pause};
      P.play=function(){const v=this;Object.defineProperty(v,'paused',{configurable:true,get:()=>false});
        Object.defineProperty(v,'readyState',{configurable:true,get:()=>4});setTimeout(()=>v.dispatchEvent(new Event('playing')),30);return Promise.resolve()};
      P.pause=function(){Object.defineProperty(this,'paused',{configurable:true,get:()=>true})};
      ['bg-motion','art-motion'].forEach(id=>$(id).addEventListener('error',e=>e.stopImmediatePropagation(),true));
      motionFailed.clear();motionUpdate()})()`);
    await until("$('art-motion').classList.contains('on')&&document.body.classList.contains('motion-square')");
    await sleep(700);
    const square=await evaluate("(()=>{const a=$('art').getBoundingClientRect(),v=$('art-motion').getBoundingClientRect();"+
      "return {art:[a.left,a.top,a.width,a.height],video:[v.left,v.top,v.width,v.height],delta:Math.abs(a.left-v.left)+Math.abs(a.top-v.top)+Math.abs(a.width-v.width)+Math.abs(a.height-v.height)}})()");
    assert.ok(square.delta<3,JSON.stringify(square));
    check(name,'desktop: square motion video lies exactly over the cover while playing',square);
    await fixture({state:{playing:false}});await evaluate('poll()');
    await until("!$('art-motion').classList.contains('on')&&$('art-motion').paused");
    await fixture({state:{playing:true}});await evaluate('poll()');await until("$('art-motion').classList.contains('on')");
    await evaluate('toggleLyrics()');await until("!$('art-motion').classList.contains('on')");
    await evaluate('toggleLyrics()');await until("$('art-motion').classList.contains('on')");
    check(name,'motion video fades out on pause and while lyrics are open, then resumes');
    await viewport(390,844,true);await evaluate('motionUpdate()');
    await until("$('bg-motion').classList.contains('on')&&document.body.classList.contains('motion-tall')&&!$('art-motion').classList.contains('on')");
    const tall=await evaluate("(()=>{const a=$('art'),r=a.getBoundingClientRect();return {visibility:getComputedStyle(a).visibility,width:r.width,height:r.height,"+
      "shade:getComputedStyle($('bg-shade')).opacity,backdropStopped:BG.frame===0}})()");
    assert.equal(tall.visibility,'hidden');assert.ok(tall.width>100&&tall.height>100,JSON.stringify(tall));
    check(name,'phone portrait: tall motion video full screen, cover hidden but keeps its box',tall);
    await evaluate("motionEnabled=true;toggleMotion()");
    await until("!$('bg-motion').classList.contains('on')&&!document.body.classList.contains('motion-tall')&&getComputedStyle($('art')).visibility==='visible'");
    assert.equal(await evaluate("localStorage.getItem('remote.motionArtwork')"),'false');
    await evaluate("toggleMotion()");
    await cdp('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]});await sleep(200);
    await until("!$('bg-motion').classList.contains('on')");
    await cdp('Emulation.setEmulatedMedia',{features:[]});
    await evaluate(`(()=>{const P=HTMLMediaElement.prototype;P.play=function(){return Promise.reject(new DOMException('blocked','NotAllowedError'))};
      ['bg-motion','art-motion'].forEach(id=>{const v=$(id);v.pause();delete v.dataset.src;v.classList.remove('on')});document.body.classList.remove('motion-tall');motionFailed.clear();motionUpdate()})()`);
    await until("motionFailed.size>0&&!document.body.classList.contains('motion-tall')");
    check(name,'motion switch, reduced motion and a refused play() all fall back to the backdrop');
    await evaluate(`(()=>{const P=HTMLMediaElement.prototype;P.play=window.__media.play;P.pause=window.__media.pause;
      ['bg-motion','art-motion'].forEach(id=>{const v=$(id);delete v.paused;delete v.readyState})})()`);
    await shot('catalog-'+name+'-now-390.png');
    await viewport(1440,900,false);

    // Title -> album page with the playing track highlighted; artist -> artist page; back stack.
    await evaluate("$('t').focus()");await key('Enter','Enter',13);
    await until("view==='album'&&!!$('alb-body').querySelector('.alb-hero')");
    const album=await evaluate("(()=>{const b=$('alb-body');return {title:b.querySelector('.page-title').textContent,tracks:b.querySelectorAll('.track').length,"+
      "current:b.querySelector('.track.current')?.dataset.song,locked:b.querySelector('.track[data-song=\"2003\"]').disabled,back:!$('hbtn').hidden,"+
      "focus:document.activeElement.className,desc:!!b.querySelector('.expand'),foot:b.querySelector('.alb-foot').textContent}})()");
    assert.equal(album.title,'Catalog Album');assert.equal(album.tracks,3);assert.equal(album.current,'2001');
    assert.equal(album.locked,true);assert.ok(album.back&&album.desc&&album.foot.includes('Fixture Records'),JSON.stringify(album));
    check(name,'keyboard Enter on the title opens its album, current track highlighted',album);
    await evaluate("$('alb-body').querySelector('.expand button').click()");
    assert.ok(await evaluate("$('alb-body').querySelector('.expand').classList.contains('open')&&$('alb-body').querySelector('.expand button').getAttribute('aria-expanded')==='true'"));
    assert.ok(await evaluate(NO_OVERFLOW));await shot('catalog-'+name+'-album-1440.png');
    await evaluate("[...$('alb-body').querySelectorAll('.page-artists button')].find(b=>b.textContent==='Catalog Artist').click()");
    await until("view==='artist'&&!!$('art-body').querySelector('.art-hero')&&navStack.length===1");
    const artist=await evaluate("(()=>{const b=$('art-body');return {name:b.querySelector('.page-title').textContent,top:b.querySelectorAll('.track').length,"+
      "shelves:b.querySelectorAll('.shelf').length,bio:!!b.querySelector('.expand'),tint:getComputedStyle($('view-art')).getPropertyValue('--tint').trim()}})()");
    assert.equal(artist.name,'Catalog Artist');assert.equal(artist.top,1);assert.ok(artist.shelves>=2&&artist.bio&&artist.tint.startsWith('rgb('),JSON.stringify(artist));
    assert.ok(await evaluate(NO_OVERFLOW));await shot('catalog-'+name+'-artist-1440.png');
    check(name,'album artist opens the artist page: bio, top songs, shelves, key-color tint',artist);
    await key('Escape','Escape',27);await until("view==='album'&&navStack.length===0&&$('alb-body').dataset.id==='1001'");
    await evaluate("$('hbtn').click()");await until("view==='now'&&navStack.length===0&&$('hbtn').hidden");
    check(name,'Escape and the back button unwind album/artist pages to now playing');

    // Several artists: the artist line opens a chooser.
    await fixture({state:{title:'Second Song',artist:'Catalog Artist & Guest Artist — Catalog Album',lyric_artist:'Catalog Artist & Guest Artist',dur:200}});
    await evaluate('poll()');await until("nowCatalog.state==='matched'&&nowCatalog.match.id==='2002'&&!!nowCatalog.album");
    await evaluate("$('a').click()");await until("!$('artist-choice').hidden");
    assert.deepEqual(await evaluate("[...$('artist-choice-list').querySelectorAll('b')].map(b=>b.textContent)"),['Catalog Artist','Guest Artist']);
    await key('Escape','Escape',27);await until("$('artist-choice').hidden&&view==='now'");
    await evaluate("$('a').click()");await until("!$('artist-choice').hidden");
    await evaluate("[...$('artist-choice-list').querySelectorAll('button')][1].click()");
    await until("view==='artist'&&!!$('art-body').querySelector('.art-hero.no-image')");
    assert.equal(await evaluate("$('art-body').querySelector('.page-title').textContent"),'Guest Artist');
    await key('Escape','Escape',27);await until("view==='now'");
    check(name,'two artists: chooser lists both, Escape cancels, choice opens that artist (iTunes fallback page)');

    // Search: keyboard entry, debounce, kinds, recent searches, states.
    await evaluate("$('sbtn').focus()");await key('Enter','Enter',13);
    await until("view==='search'&&document.activeElement===$('sq')&&$('htitle').textContent==='搜索'");
    await fixture({clear_calls:true});
    await cdp('Input.insertText',{text:'Catalog'});
    await sleep(250);assert.equal((await calls()).filter(c=>c.startsWith('/catalog/search')).length,0);
    await until("$('src-list').querySelectorAll('.row.catalog-play').length===2",4000);
    assert.equal((await calls()).filter(c=>c.startsWith('/catalog/search')).length,1);
    check(name,'search runs once after typing pauses for 500 ms');
    await evaluate("$('src-kinds').querySelector('[data-kind=album]').click()");await until("$('src-list').textContent.includes('Catalog Album')&&!$('src-list').querySelector('.catalog-play')");
    await evaluate("$('src-kinds').querySelector('[data-kind=album]').focus()");await key('ArrowRight','ArrowRight',39);
    await until("searchKind==='artist'&&$('src-list').querySelector('.avatar')!==null&&document.activeElement.dataset.kind==='artist'");
    await evaluate("$('src-kinds').querySelector('[data-kind=song]').click()");await until("$('src-list').querySelectorAll('.row.catalog-play').length===2");
    await evaluate("$('sq').focus()");await key('Enter','Enter',13);await until("JSON.parse(localStorage.getItem('remote.recentSearches')||'[]')[0]==='Catalog'");
    await evaluate("$('sq').value='';$('sq').dispatchEvent(new Event('input'))");
    await until("$('src-list').textContent.includes('最近搜索')&&$('src-list').textContent.includes('Catalog')");
    check(name,'kinds switch by click and arrow keys; Enter saves a recent search shown when empty');
    for(const [term,error,text] of [['none','','没有找到'],['x','rate_limited','42 秒后再试'],['y','unavailable','暂时无法连接']]){
      await fixture({catalog_search_error:error});await evaluate("$('sq').value="+JSON.stringify(term)+";runSearch(false)");
      await until("$('src-status').textContent.includes("+JSON.stringify(text)+")");
    }
    assert.ok(await evaluate("!!$('src-status').querySelector('button')"));
    await fixture({catalog_search_error:''});await evaluate("$('src-status').querySelector('button').click()");
    await until("$('src-list').querySelectorAll('.row.catalog-play').length===2");
    check(name,'search states: no results, rate limited with retry time, failure and retry');
    assert.ok(await evaluate(NO_OVERFLOW));await shot('catalog-'+name+'-search-1440.png');

    // Search result -> play in Apple Music -> back to now playing (like queue selection).
    await fixture({clear_calls:true,state:{title:'Before play',playing:false}});await evaluate('poll()');
    await evaluate("$('src-list').querySelector('.row.catalog-play').click();$('src-list').querySelector('.row.catalog-play').click()");
    assert.ok(await evaluate("catalogPlaying&&$('src-list').querySelector('.row.catalog-play').disabled"));
    await until("view==='now'&&!catalogPlaying&&$('t').textContent==='Catalog Song'");
    const played=(await calls()).filter(c=>c==='/catalog/play').length;assert.equal(played,1);
    assert.ok(await evaluate("$('control-error').hidden&&navStack.length===0"));
    check(name,'search song plays once (double tap ignored), returns to now playing and refreshes');
    // Failure stays on the page with a visible message; locked track stays disabled.
    await fixture({catalog_play_error:'在 Apple Music 专辑页中找不到这首歌'});
    await evaluate("openAlbum('1001')");await until("view==='album'&&!!$('alb-body').querySelector('.alb-hero')");
    await evaluate("$('alb-body').querySelector('.track[data-song=\"2002\"]').click()");
    await until("!catalogPlaying&&$('control-error').textContent.includes('找不到这首歌')");
    assert.ok(await evaluate("view==='album'&&!$('alb-body').querySelector('.track[data-song=\"2002\"]').disabled&&$('alb-body').querySelector('.track[data-song=\"2003\"]').disabled"));
    await fixture({catalog_play_error:'',clear_calls:true});
    await evaluate("[...$('alb-body').querySelectorAll('.alb-foot .pill')].pop().click()");
    await until("$('alb-body').querySelector('.alb-foot .pill span').textContent==='已在电脑上打开'");
    assert.ok((await calls()).includes('/catalog/open'));
    await evaluate("$('alb-body').querySelector('.alb-actions .pill').click()");await until("view==='now'&&!catalogPlaying");
    check(name,'album page: play failure is shown in place; open on computer; album play returns to now playing');

    // Phone width layouts and screenshots.
    await viewport(390,844,true);
    await evaluate("openSearch('Catalog','song')");await until("view==='search'&&$('src-list').querySelectorAll('.row.catalog-play').length===2");
    assert.ok(await evaluate(NO_OVERFLOW));await shot('catalog-'+name+'-search-390.png');
    await evaluate("openAlbum('1001','2002')");await until("view==='album'&&!!$('alb-body').querySelector('.track.flash')");
    assert.ok(await evaluate(NO_OVERFLOW));await shot('catalog-'+name+'-album-390.png');
    await evaluate("openArtist('3001')");await until("view==='artist'&&!!$('art-body').querySelector('.art-hero')");
    assert.ok(await evaluate(NO_OVERFLOW));await shot('catalog-'+name+'-artist-390.png');
    await viewport(320,568,true);assert.ok(await evaluate(NO_OVERFLOW));
    await evaluate("goBack()");await until("view==='album'");assert.ok(await evaluate(NO_OVERFLOW));
    check(name,'search, album and artist pages fit 390 px and 320 px without horizontal scrolling');
    await evaluate("show('now')");await viewport(1440,900,false);

    // Losing the WebGL context switches to the 2D canvas without a reload.
    await evaluate("BG.gl.gl.getExtension('WEBGL_lose_context').loseContext()");
    await until("!BG.gl&&!$('bg-canvas').hidden&&$('bg-gl').hidden");
    const flat=await evaluate(RANGE);assert.equal(flat.mode,'2d');assert.ok(flat.range>10,JSON.stringify(flat));
    check(name,'WebGL context loss falls back to the 2D backdrop',flat);
    assert.deepEqual(b.exceptions,[]);check(name,'no JavaScript exceptions');
  }finally{await b.close()}
  // Software-only WebGL (GPU disabled: WARP) is refused in favour of the 2D canvas.
  await fixture({state:CATALOG_SONG,catalog_play_error:''});
  const soft=await browser(executable,name+'-software',true);
  try{
    await soft.cdp('Page.navigate',{url:base});await soft.until("typeof poll==='function'&&nowCatalog.state==='matched'");
    await soft.until("!!BG.to");const flat=await soft.evaluate(RANGE);
    assert.equal(flat.mode,'2d');assert.ok(flat.range>10&&await soft.evaluate("!$('bg-canvas').hidden&&$('bg-gl').hidden"),JSON.stringify(flat));
    assert.deepEqual(soft.exceptions,[]);check(name,'software-only WebGL (--disable-gpu) uses the 2D backdrop',flat);
  }finally{await soft.close()}
}
(async()=>{
  try{
    await startServer();
    await run('edge','C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe');
    await run('chrome','C:/Program Files/Google/Chrome/Application/chrome.exe');
  }finally{
    await stopServer();
    fs.writeFileSync(path.join(output,'catalog-browser-results.json'),JSON.stringify({
      method:'Real headless Edge and Chrome via CDP; loopback fixture for catalog data and playback; motion playback success is modelled by stubbing the media element because the fixture has no video; no network, no Apple Music, no system audio.',
      results},null,2));
  }
})().catch(error=>{console.error(error);process.exitCode=1});
