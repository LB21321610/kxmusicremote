/* Real production server + Apple Music. Changes playback, then restores the
   original recording via history and its position. Never changes the library or volume. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const {spawn}=require('node:child_process');
const base='http://127.0.0.1:8765',out=path.join(__dirname,'artifacts');
const file=path.join(out,'queue-artist-live.json');
const report=JSON.parse(fs.readFileSync(file,'utf8'));
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const live={method:'Headless Windows Chrome, production :8765, real Apple Music UIA and SMTC; no Mac/iOS device',started:new Date().toISOString()};
report.browser_and_playback=live;
async function http(route,post=false){
  const r=await fetch(base+route,{method:post?'POST':'GET',signal:AbortSignal.timeout(20000)});
  const text=await r.text();return {status:r.status,body:text?JSON.parse(text):null};
}
async function status(){return (await http('/status')).body}
(async()=>{
  const profile=fs.mkdtempSync(path.join(os.tmpdir(),'queue-artist-live-'));
  const browser=spawn('C:/Program Files/Google/Chrome/Application/chrome.exe',[
    '--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check',
    '--disable-background-timer-throttling','--disable-renderer-backgrounding',
    '--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],{windowsHide:true,stdio:'ignore'});
  let socket,cdp,original,clicked=false;
  try{
    for(let i=0;i<100&&!fs.existsSync(path.join(profile,'DevToolsActivePort'));i++)await sleep(100);
    const port=fs.readFileSync(path.join(profile,'DevToolsActivePort'),'utf8').split('\n')[0];
    const target=(await(await fetch('http://127.0.0.1:'+port+'/json/list')).json()).find(t=>t.type==='page');
    socket=new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
    let id=0;const pending=new Map(),exceptions=[];live.requests=[];
    socket.onmessage=event=>{
      const msg=JSON.parse(event.data);
      if(msg.method==='Runtime.exceptionThrown')exceptions.push(msg.params.exceptionDetails);
      if(msg.method==='Network.responseReceived'&&msg.params.response.url===base+'/queue/play')
        live.requests.push({requestId:msg.params.requestId,status:msg.params.response.status});
      if(msg.id&&pending.has(msg.id)){
        const entry=pending.get(msg.id);pending.delete(msg.id);clearTimeout(entry.timer);
        msg.error?entry.reject(Error(JSON.stringify(msg.error))):entry.resolve(msg.result);
      }
    };
    cdp=(method,params={})=>new Promise((resolve,reject)=>{
      const request=++id,timer=setTimeout(()=>{pending.delete(request);reject(Error('CDP timeout '+method))},25000);
      pending.set(request,{resolve,reject,timer});socket.send(JSON.stringify({id:request,method,params}));
    });
    const evaluate=async expression=>{
      const r=await cdp('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});
      if(r.exceptionDetails)throw Error(r.exceptionDetails.exception?.description||r.exceptionDetails.text);
      return r.result.value;
    };
    const until=async(expression,timeout=15000)=>{
      const end=Date.now()+timeout;
      while(Date.now()<end){if(await evaluate(expression))return;await sleep(100)}
      throw Error('Timed out: '+expression);
    };
    await cdp('Runtime.enable');await cdp('Page.enable');await cdp('Network.enable');
    await cdp('Emulation.setFocusEmulationEnabled',{enabled:true});
    await cdp('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]});
    await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:2,mobile:true});
    await cdp('Page.navigate',{url:base});
    await until("typeof openQue==='function'");await evaluate('openQue()');
    await until("$('que-list').querySelectorAll('.row').length>0");
    live.queue=(await http('/queue')).body;assert(live.queue.ok);
    live.display=await evaluate("Array.from($('que-list').querySelectorAll('.row')).map(r=>({title:r.querySelector('b').textContent,artist:r.querySelector('.rt>span').textContent,aria:r.getAttribute('aria-label')}))");
    assert.equal(live.display.length,live.queue.items.length);
    live.queue.items.forEach((item,index)=>assert.deepEqual(live.display[index],{
      title:item.t,artist:item.a,aria:'播放 '+item.t+' · '+item.a}));
    const captured=report.read_after_restart.native_rows;
    const index=live.queue.items.findIndex(item=>captured.some(row=>row.has_icon&&row.parsed.t===item.t&&row.parsed.a===item.a));
    assert(index>=0,'Previously affected song remains in queue');
    assert(live.queue.items.some(item=>captured.some(row=>!row.has_icon&&row.parsed.t===item.t&&row.parsed.a===item.a)),
      'Normal song also checked');
    for(const [width,height,mobile] of [[390,844,true],[1440,900,false]]){
      await cdp('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:mobile?2:1,mobile});await sleep(500);
      const shot=await cdp('Page.captureScreenshot',{format:'png'});
      fs.writeFileSync(path.join(out,'queue-artist-live-'+width+'.png'),Buffer.from(shot.data,'base64'));
    }
    original=await status();live.before_selection=original;
    live.selected={index,item:live.queue.items[index]};
    assert.notEqual(original.title,live.selected.item.t);
    fs.writeFileSync(file,JSON.stringify(report,null,2));
    clicked=true;
    await evaluate(`$('que-list').querySelectorAll('.row')[${index}].click()`);
    await until('!queuePlaying',20000);
    live.page_after_selection=await evaluate("({view,error:$('control-error').textContent,errorHidden:$('control-error').hidden})");
    assert.equal(live.page_after_selection.view,'now');assert(live.page_after_selection.errorHidden);
    live.after_selection=await status();
    assert.equal(live.after_selection.title,live.selected.item.t);
    const [artist,album]=live.selected.item.a.split(' — ');
    assert.equal(live.after_selection.lyric_artist,artist);
    assert.equal(live.after_selection.album,album);
    const [minutes,seconds]=live.selected.item.d.split(':').map(Number);
    assert(Math.abs(live.after_selection.dur-minutes*60-seconds)<=2);
    assert(live.after_selection.playing);
    for(const request of live.requests){
      const response=await cdp('Network.getResponseBody',{requestId:request.requestId});
      request.body=JSON.parse(response.body);
      assert.equal(request.status,200);assert(request.body.ok);
    }
    assert.equal(live.requests.length,1);assert.deepEqual(exceptions,[]);
    live.exceptions=exceptions;live.ok=true;
    console.log('PASS actual queue subtitles and aria-labels; real native song activation, strict SMTC confirmation and return to player');
  }catch(error){live.error=String(error.stack||error);process.exitCode=1;console.error(live.error)}
  finally{
    if(clicked&&original){
      try{
        live.restore={commands:[],samples:[]};
        const matches=s=>s.title===original.title&&s.lyric_artist===original.lyric_artist
          &&s.album===original.album&&Math.abs(s.dur-original.dur)<=2;
        let current=await status();
        for(let attempt=0;attempt<4&&!matches(current);attempt++){
          // Apple Music's Previous restarts a track after its first seconds.
          // Seek to zero first so each command actually goes to history.
          for(const route of ['/cmd/seek/0','/cmd/prev']){
            const response=await http(route,true);
            live.restore.commands.push({route,...response});assert.equal(response.status,204);
          }
          const deadline=Date.now()+2000;
          do{
            await sleep(150);current=await status();live.restore.samples.push(current);
            if(matches(current))break;
          }while(Date.now()<deadline);
        }
        assert(matches(current),'Previous-track history restored exact original recording');
        const route='/cmd/seek/'+Math.floor(original.pos);
        live.restore.commands.push({route,...await http(route,true)});
        if(current.playing!==original.playing)
          live.restore.commands.push({route:'/cmd/toggle',...await http('/cmd/toggle',true)});
        await sleep(500);live.restored=await status();
        live.restore.ok=matches(live.restored)&&live.restored.playing===original.playing
          &&Math.abs(live.restored.pos-original.pos)<6&&live.restored.vol===original.vol&&live.restored.muted===original.muted;
        assert(live.restore.ok,'Song, position, play state, volume and mute restored');
        console.log('PASS restored original song, position, play state, volume and mute');
      }catch(error){live.restore=live.restore||{};live.restore.error=String(error.stack||error);process.exitCode=1;console.error(live.restore.error)}
    }
    if(socket)try{socket.send(JSON.stringify({id:999999,method:'Browser.close'}))}catch{}
    if(socket)socket.close();browser.kill();
    live.finished=new Date().toISOString();fs.writeFileSync(file,JSON.stringify(report,null,2));
  }
})();
