/* Two real browser clients, production settings store, fixture playback/DeepL. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const {spawn}=require('node:child_process');
const root=path.resolve(__dirname,'..'),base='http://127.0.0.1:18767';
const workspace=fs.mkdtempSync(path.join(os.tmpdir(),'controls-browser-'));
const settings=path.join(workspace,'settings.json'),results=[];
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
let server;
async function startServer(cwd){
  server=spawn(path.join(root,'.venv/Scripts/python.exe'),[path.join(__dirname,'lyrics_fixture_server.py')],
    {cwd,windowsHide:true,env:{...process.env,REMOTE_FIXTURE_PORT:'18767',REMOTE_FIXTURE_SETTINGS:settings},stdio:'ignore'});
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
function check(label){results.push({check:label,passed:true});console.log(label)}
async function browser(executable,name){
  const profile=path.join(workspace,name);
  const process=spawn(executable,['--headless=new','--disable-gpu','--no-first-run',
    '--disable-background-timer-throttling','--disable-renderer-backgrounding','--disable-backgrounding-occluded-windows',
    '--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],{windowsHide:true,stdio:'ignore'});
  for(let i=0;i<100&&!fs.existsSync(path.join(profile,'DevToolsActivePort'));i++)await sleep(100);
  const port=fs.readFileSync(path.join(profile,'DevToolsActivePort'),'utf8').split('\n')[0];
  const target=(await(await fetch('http://127.0.0.1:'+port+'/json/list')).json()).find(t=>t.type==='page');
  const socket=new WebSocket(target.webSocketDebuggerUrl),pending=new Map(),exceptions=[];let id=0;
  await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
  socket.onmessage=event=>{
    const msg=JSON.parse(event.data);
    if(msg.method==='Runtime.exceptionThrown')exceptions.push(msg.params.exceptionDetails);
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
  async function until(expression,timeout=10000){const end=Date.now()+timeout;
    while(Date.now()<end){if(await evaluate(expression))return;await sleep(60)}throw Error('Timed out: '+expression)}
  await cdp('Runtime.enable');await cdp('Page.enable');
  // Headless Edge can randomly report a busy page as hidden; model a viewed page.
  await cdp('Emulation.setFocusEmulationEnabled',{enabled:true});
  // Decoration-free behaviour checks (motion is covered by verify_lyrics_browser.cjs).
  await cdp('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]});
  async function open(){await cdp('Page.navigate',{url:base});await until('offsetConfirmed!==null&&lyricTrack!==null');
    await until('dur===180')}
  return {name,cdp,evaluate,until,open,exceptions,async close(){try{await cdp('Browser.close')}catch{}socket.close();process.kill()}};
}
(async()=>{
  let a;
  try{
    await startServer(workspace);
    for(const [name,executable] of [
      ['edge','C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'],
      ['chrome','C:/Program Files/Google/Chrome/Application/chrome.exe']]){
      await fixture({state:{pos:10,playing:false},command_error:'',queue_error:'',clear_calls:true});
      a=await browser(executable,name);await a.open();
      const rect=await a.evaluate("(()=>{const r=$('pr').getBoundingClientRect();return {x:r.x,y:r.y+r.height/2,width:r.width}})()");
      await a.cdp('Input.dispatchMouseEvent',{type:'mousePressed',x:rect.x+rect.width*.15,y:rect.y,button:'left',clickCount:1});
      await a.cdp('Input.dispatchMouseEvent',{type:'mouseMoved',x:rect.x+rect.width*.65,y:rect.y,button:'left',buttons:1});
      await a.cdp('Input.dispatchMouseEvent',{type:'mouseReleased',x:rect.x+rect.width*.65,y:rect.y,button:'left',clickCount:1});
      await a.until('!scrubbing&&playbackPosition()>100');
      const state=await(await fetch(base+'/__test')).json();
      assert(state.calls.some(c=>c.startsWith('/cmd/seek/')));
      assert(state.state.pos>100&&state.state.pos<130);check(name+': real pointer drag sends seek and updates progress');
      await fixture({state:{pos:40},command_error:'fixture missing slider'});
      await a.evaluate('seekTo(90)');
      await a.until("!$('control-error').hidden&&$('control-error').textContent.includes('missing slider')&&Math.abs(playbackPosition()-40)<1");
      check(name+': seek failure is visible and progress rolls back to server');
      await fixture({command_error:''});await a.evaluate('seekTo(60)');
      await a.until("$('control-error').hidden");check(name+': successful retry clears control error');
      await fixture({queue_error:'<img src=x onerror=alert(1)> fixture queue error'});
      await a.evaluate('openQue()');
      assert.equal(await a.evaluate("$('que-list').querySelectorAll('img').length"),0);
      assert(await a.evaluate("$('que-list').textContent.includes('fixture queue error')"));
      await fixture({queue_error:''});await a.evaluate("$('que-list').querySelector('button').click()");
      await a.until("$('que-list').querySelectorAll('.row').length===1");check(name+': queue failure is escaped and retry renders rows');
      await fixture({command_error:'fixture selection failed',clear_calls:true});
      await a.evaluate("$('que-list').querySelector('.row').click();$('que-list').querySelector('.row').click()");
      assert.equal(await a.evaluate("$('que-list').querySelector('.row').disabled"),true);
      await a.until("!queuePlaying&&$('control-error').textContent.includes('selection failed')");
      assert.equal((await(await fetch(base+'/__test')).json()).calls.filter(c=>c==='/queue/play').length,1);
      assert.equal(await a.evaluate('view'),'que');check(name+': selection failure stays on queue and double taps send one request');
      await fixture({command_error:'stale queue',clear_calls:true});
      await a.evaluate("$('que-list').querySelector('.row').click()");
      await a.until("!queuePlaying&&$('que-list').querySelector('.row')&&$('control-error').textContent==='stale queue'");
      assert((await(await fetch(base+'/__test')).json()).calls.includes('/queue'));
      check(name+': stale selection refreshes queue without retrying playback');
      await fixture({command_error:'',clear_calls:true,state:{title:'Before selection'}});
      await a.evaluate('openQue()');
      await a.evaluate("$('que-list').querySelector('.row').focus()");
      await a.cdp('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',windowsVirtualKeyCode:13,text:'\r',unmodifiedText:'\r'});
      await a.cdp('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
      try{await a.until("view==='now'&&!queuePlaying&&$('control-error').hidden")}
      catch(error){console.error(await a.evaluate("({view,queuePlaying,error:$('control-error').textContent,active:document.activeElement.outerHTML})"));throw error}
      assert.equal((await(await fetch(base+'/__test')).json()).state.title,'Test Song');
      check(name+': keyboard selection plays exact submitted item and returns to player');
      await fixture({state:{ok:false}});await a.evaluate('poll()');
      assert.equal(await a.evaluate("$('prog').style.visibility"),'hidden');
      await fixture({state:{ok:true}});check(name+': missing app clears stale progress');
      assert.deepEqual(a.exceptions,[]);await a.close();a=null;
    }
  }finally{
    if(a)await a.close();await stopServer();
    fs.writeFileSync(path.join(root,'tests/artifacts/controls-browser-results.json'),JSON.stringify({method:'Isolated Edge and Chrome, fixture playback only; no system audio changes',results},null,2));
  }
})().catch(error=>{console.error(error);process.exitCode=1});
