/* Real-data screenshots for verify_catalog_live.py: headless Chrome against the
   temporary 127.0.0.1:8799 server while Apple Music plays a song whose album
   has motion artwork. Checks that the real motion video actually plays. */
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const {spawn}=require('node:child_process');
const base='http://127.0.0.1:8799',output=path.join(__dirname,'artifacts');
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
  const profile=fs.mkdtempSync(path.join(os.tmpdir(),'catalog-live-'));
  const chrome=spawn('C:/Program Files/Google/Chrome/Application/chrome.exe',['--headless=new','--no-first-run',
    '--no-default-browser-check','--autoplay-policy=no-user-gesture-required','--disable-background-timer-throttling',
    '--disable-renderer-backgrounding','--window-size=1440,1000','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],
    {windowsHide:true,stdio:'ignore'});
  const report={ok:false,screens:[]};let socket;
  try{
    for(let i=0;i<100&&!fs.existsSync(path.join(profile,'DevToolsActivePort'));i++)await sleep(100);
    const port=fs.readFileSync(path.join(profile,'DevToolsActivePort'),'utf8').split('\n')[0];
    const target=(await(await fetch('http://127.0.0.1:'+port+'/json/list')).json()).find(t=>t.type==='page');
    socket=new WebSocket(target.webSocketDebuggerUrl);await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
    let id=0;const pending=new Map(),exceptions=[];
    socket.onmessage=event=>{const msg=JSON.parse(event.data);
      if(msg.method==='Runtime.exceptionThrown')exceptions.push(msg.params.exceptionDetails.text+' '+(msg.params.exceptionDetails.exception?.description||''));
      if(msg.id&&pending.has(msg.id)){pending.get(msg.id)(msg);pending.delete(msg.id)}};
    const cdp=(method,params={})=>new Promise(resolve=>{const i=++id;pending.set(i,resolve);socket.send(JSON.stringify({id:i,method,params}))});
    const evaluate=async expression=>{const r=await cdp('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});
      if(r.result.exceptionDetails)throw Error(r.result.exceptionDetails.exception?.description||r.result.exceptionDetails.text);return r.result.result.value};
    const until=async(expression,timeout=15000)=>{const end=Date.now()+timeout;while(Date.now()<end){if(await evaluate(expression))return true;await sleep(150)}return false};
    const shot=async name=>{await sleep(900);const r=await cdp('Page.captureScreenshot',{format:'png'});
      fs.writeFileSync(path.join(output,name),Buffer.from(r.result.data,'base64'));report.screens.push(name)};
    const viewport=async(width,height,mobile)=>{await cdp('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:mobile?3:1,mobile});await sleep(300)};
    await cdp('Runtime.enable');await cdp('Page.enable');await cdp('Emulation.setFocusEmulationEnabled',{enabled:true});
    await viewport(1440,900,false);await cdp('Page.navigate',{url:base});
    report.matched=await until("typeof nowCatalog==='object'&&nowCatalog.state==='matched'&&!!nowCatalog.album",20000);
    report.song=await evaluate("nowCatalog.match&&{id:nowCatalog.match.id,album:nowCatalog.match.album_id,title:$('t').textContent}");
    report.backdrop=await evaluate("backdropSample().mode");
    report.squarePlaying=await until("$('art-motion').classList.contains('on')",25000);
    report.square=await evaluate("(async()=>{const v=$('art-motion'),t=v.currentTime;await new Promise(r=>setTimeout(r,1500));"+
      "return {src:v.currentSrc.split('/').pop(),hls:v.currentSrc.endsWith('.m3u8'),width:v.videoWidth,height:v.videoHeight,advanced:v.currentTime>t,readyState:v.readyState}})()");
    await shot('catalog-live-now-1440.png');
    await viewport(390,844,true);await evaluate('motionUpdate()');
    report.tallPlaying=await until("$('bg-motion').classList.contains('on')&&document.body.classList.contains('motion-tall')",25000);
    report.tall=await evaluate("(async()=>{const v=$('bg-motion'),t=v.currentTime;await new Promise(r=>setTimeout(r,1500));"+
      "return {src:v.currentSrc.split('/').pop(),width:v.videoWidth,height:v.videoHeight,advanced:v.currentTime>t,art:getComputedStyle($('art')).visibility}})()");
    await shot('catalog-live-now-390.png');
    await evaluate('toggleMotion()');await until("!document.body.classList.contains('motion-tall')",5000);
    await shot('catalog-live-fluid-390.png');await viewport(1440,900,false);await shot('catalog-live-fluid-1440.png');
    await evaluate('toggleMotion()');
    await evaluate("openAlbum(nowCatalog.match.album_id,nowCatalog.match.id)");
    report.album=await until("view==='album'&&!!$('alb-body').querySelector('.alb-hero')",20000);
    report.albumMotion=await until("!!$('alb-body').querySelector('.page-motion.on')",15000);
    await shot('catalog-live-album-1440.png');await viewport(390,844,true);await evaluate("$('alb-body').scrollTop=0");await shot('catalog-live-album-390.png');
    await evaluate("openArtist(nowCatalog.album.artists[0].id)");
    report.artist=await until("view==='artist'&&!!$('art-body').querySelector('.art-hero img')",20000);
    await shot('catalog-live-artist-390.png');await viewport(1440,900,false);await shot('catalog-live-artist-1440.png');
    await evaluate("openSearch('深呼吸 羽泉','song')");
    report.search=await until("$('src-list').querySelectorAll('.row.catalog-play').length>0",20000);
    await viewport(390,844,true);await shot('catalog-live-search-390.png');
    await evaluate("show('now')");
    report.exceptions=exceptions;
    report.ok=!!(report.matched&&report.squarePlaying&&report.square.advanced&&report.tallPlaying&&report.tall.advanced
      &&report.album&&report.artist&&report.search&&!exceptions.length);
  }catch(error){report.error=String(error&&error.stack||error)}
  finally{
    try{socket&&socket.send(JSON.stringify({id:999999,method:'Browser.close'}))}catch{}
    await sleep(300);chrome.kill();
    fs.writeFileSync(path.join(output,'catalog-live-screens.json'),JSON.stringify(report,null,1));
    console.log(JSON.stringify(report));
  }
})();
