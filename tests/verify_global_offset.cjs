/* Two real browser clients, production settings store, fixture playback/DeepL. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const {spawn}=require('node:child_process');
const root=path.resolve(__dirname,'..'),base='http://127.0.0.1:18766';
const workspace=fs.mkdtempSync(path.join(os.tmpdir(),'global-offset-'));
const settings=path.join(workspace,'settings.json'),results=[];
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
let server;
async function startServer(cwd){
  server=spawn(path.join(root,'.venv/Scripts/python.exe'),[path.join(__dirname,'lyrics_fixture_server.py')],
    {cwd,windowsHide:true,env:{...process.env,REMOTE_FIXTURE_PORT:'18766',REMOTE_FIXTURE_SETTINGS:settings},stdio:'ignore'});
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
    await evaluate('if(!lyricsVisible)toggleLyrics()');await until('lyricLines.length===60')}
  return {name,cdp,evaluate,until,open,exceptions,async close(){try{await cdp('Browser.close')}catch{}socket.close();process.kill()}};
}
(async()=>{
  let a,b;
  try{
    await startServer(workspace);
    await fixture({state:{title:'Global offset song',pos:5.5,playing:false},translation_case:'english'});
    a=await browser('C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe','edge');
    b=await browser('C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe','chrome');
    await a.open();await b.open();
    assert.equal(await a.evaluate("$('lyrics-offset').textContent"),'无偏移');
    // Keep old per-track data intact; it must never become the active offset.
    await a.evaluate("localStorage.setItem('remote.lyricCalibration.'+lyricKey,JSON.stringify({candidate:'auto',offset:9,legacy:'keep'}));loadLyricCalibration()");
    assert.equal(await a.evaluate('lyricOffset'),0);
    check('new clients read server default; legacy localStorage offset is ignored');
    await a.evaluate('adjustLyricOffset(-.5)');
    assert.equal(await a.evaluate('lyricActive'),2); // 6-second row shows at 5.5 seconds.
    assert.equal(await a.evaluate('playbackPosition()'),5.5);
    await a.until('!offsetPending&&!offsetSaving&&offsetConfirmed.lyric_offset===-.5');
    await b.until('lyricOffset===-.5&&lyricActive===2');
    assert.equal(await a.evaluate("$('lyrics-offset').textContent"),'提前 0.5 秒');
    assert.equal(await b.evaluate("$('lyrics-offset').textContent"),'提前 0.5 秒');
    check('advance 0.5s appears exactly 0.5s early and automatically syncs to the second client');
    await b.evaluate('adjustLyricOffset(1)');
    await b.until('!offsetPending&&!offsetSaving&&offsetConfirmed.lyric_offset===.5');
    await a.until('lyricOffset===.5');
    await fixture({state:{pos:6}});await a.evaluate('poll()');
    assert.equal(await a.evaluate('lyricActive'),1);
    await fixture({state:{pos:6.5}});await a.evaluate('poll()');
    assert.equal(await a.evaluate('lyricActive'),2);
    assert.equal(await a.evaluate("$('lyrics-offset').textContent"),'延后 0.5 秒');
    check('delay 0.5s boundary and unchanged playback position/progress');
    for(const title of ['Second song','Third song']){
      await fixture({state:{title,pos:5.5}});await a.evaluate('poll()');await b.evaluate('poll()');
      await a.until('rawLyrics&&rawLyrics.record.trackName==='+JSON.stringify(title));
      assert.equal(await a.evaluate('lyricOffset'),.5);assert.equal(await b.evaluate('lyricOffset'),.5);
    }
    await a.evaluate('if(!translationEnabled)toggleTranslation()');
    await a.until("document.querySelectorAll('.lyric-translation').length===60");
    const raw=await a.evaluate('JSON.stringify(rawLyrics)'),translation=await a.evaluate('JSON.stringify([...derivedRows])');
    const before=(await(await fetch(base+'/__test')).json()).calls;
    await fixture({settings_delay:.4});
    await a.evaluate('adjustLyricOffset(.1)');await a.until('offsetSaving');
    await a.evaluate('for(let i=0;i<14;i++)adjustLyricOffset(.1)');
    assert.equal(await a.evaluate('lyricOffset'),2);
    await a.until('!offsetPending&&!offsetSaving&&offsetConfirmed.lyric_offset===2');
    await b.until('lyricOffset===2');
    const after=(await(await fetch(base+'/__test')).json()).calls;
    assert.equal(after.filter(p=>p.startsWith('/lyrics?')).length,before.filter(p=>p.startsWith('/lyrics?')).length);
    assert.equal(after.filter(p=>p.startsWith('/lyrics/translation')).length,before.filter(p=>p.startsWith('/lyrics/translation')).length);
    assert.equal(await a.evaluate('JSON.stringify(rawLyrics)'),raw);
    assert.equal(await a.evaluate('JSON.stringify([...derivedRows])'),translation);
    assert.ok(await a.evaluate("lyricNodes[lyricActive].querySelector('.lyric-original')&&lyricNodes[lyricActive].querySelector('.lyric-translation')"));
    check('multiple songs retain one offset; rapid in-flight edits finish identically on both devices without lyric/translation refetch');
    await fixture({settings_delay:0,settings_failure:true});
    await a.evaluate('adjustLyricOffset(.1)');
    assert.equal(await a.evaluate('lyricOffset'),2.1);
    await a.until("!offsetSaving&&!offsetPending&&$('lyrics-offset-status').textContent.includes('保存失败')");
    assert.equal(await a.evaluate('lyricOffset'),2);
    assert.equal((await(await fetch(base+'/settings/lyrics')).json()).lyric_offset,2);
    assert.ok(!(await a.evaluate("$('lyrics-offset-status').textContent")).includes('已保存'));
    check('failed save displays failure and restores confirmed server value');
    await fixture({settings_failure:false});
    await a.evaluate('adjustLyricOffset(0,true)');await a.until('!offsetPending&&!offsetSaving&&offsetConfirmed.lyric_offset===0');
    await b.until('lyricOffset===0');assert.equal(await a.evaluate("$('lyrics-offset').textContent"),'无偏移');
    // Concurrent devices start from the same revision: a stale request is rejected.
    await fixture({settings_delay:.5});
    await a.evaluate('adjustLyricOffset(-.5)');await a.until('offsetSaving');
    await b.evaluate('adjustLyricOffset(.5)');
    await a.until('!offsetSaving&&!offsetPending');await b.until('!offsetSaving&&!offsetPending');
    await a.until('lyricOffset===offsetConfirmed.lyric_offset');await b.until('lyricOffset===offsetConfirmed.lyric_offset');
    await sleep(2100);
    const concurrent=(await(await fetch(base+'/settings/lyrics')).json()).lyric_offset;
    assert.equal(concurrent,-.5);assert.equal(await a.evaluate('lyricOffset'),concurrent);assert.equal(await b.evaluate('lyricOffset'),concurrent);
    assert.ok(await b.evaluate("$('lyrics-offset-status').textContent.includes('其他设备已更新')"));
    check('reset autosaves; concurrent stale device cannot overwrite a newer revision');
    await fixture({settings_delay:0});
    await a.cdp('Page.reload');await a.until('offsetConfirmed!==null&&lyricOffset===-.5&&lyricLines.length===60');
    await a.evaluate("$('lyrics-version').value='NE-fixture';changeLyricVersion()");
    await a.until("rawLyrics&&rawLyrics.source==='网易云音乐'");
    assert.equal(await a.evaluate('lyricOffset'),-.5);
    const old=await a.evaluate("JSON.parse(localStorage.getItem('remote.lyricCalibration.'+JSON.stringify({title:'Global offset song',artist:'Artist',album:'Album',duration:180})))");
    assert.equal(old.offset,9);assert.equal(old.legacy,'keep');
    check('reload and source change retain global value and preserve legacy data');
    await stopServer();const changedCwd=path.join(workspace,'different-cwd');fs.mkdirSync(changedCwd);await startServer(changedCwd);
    assert.equal((await(await fetch(base+'/settings/lyrics')).json()).lyric_offset,-.5);
    await b.cdp('Page.reload');await b.until('offsetConfirmed!==null&&lyricOffset===-.5');
    // Close the actual page, then create a replacement in the same running browser.
    await a.cdp('Page.navigate',{url:'about:blank'});await a.open();assert.equal(await a.evaluate('lyricOffset'),-.5);
    check('service process restart from a different cwd, page reopen and refresh restore saved value');
    await fixture({state:{ok:false}});await a.evaluate('poll()');
    await b.evaluate('adjustLyricOffset(.6)');await b.until('!offsetSaving&&!offsetPending&&offsetConfirmed.lyric_offset===.1');
    await a.until('lyricOffset===.1');
    await fixture({state:{ok:true,title:'Playback check',pos:10,playing:false}});await a.evaluate('poll()');
    await a.until('lyricLines.length===60');
    await a.evaluate('seekTo(60)');assert.equal(await a.evaluate('playbackPosition()'),60);
    assert.equal(await a.evaluate('lyricActive'),19);
    await a.evaluate('pauseLyricsFollow()');await a.evaluate('adjustLyricOffset(-.1)');
    assert.ok(await a.evaluate('manualUntil>Date.now()'));
    await a.until('!offsetSaving&&!offsetPending&&offsetConfirmed.lyric_offset===0');
    await a.evaluate('resumeLyrics();c("toggle")');await a.until('playing');await sleep(300);
    assert.ok(await a.evaluate('playbackPosition()>60'));
    await a.evaluate('c("toggle")');await a.until('!playing');
    const paused=await a.evaluate('playbackPosition()');await sleep(200);assert.equal(await a.evaluate('playbackPosition()'),paused);
    check('settings sync with Apple Music closed; seek, manual scroll, play/pause controls remain functional');
    assert.deepEqual(a.exceptions,[]);assert.deepEqual(b.exceptions,[]);
    await a.cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:3,mobile:true});
    // Offset controls live in the 调整 popover inside the lyrics panel.
    await a.evaluate('scheduleLyricsLayout();toggleLyricSheet(true)');await sleep(250);
    const bounds=await a.evaluate("(()=>{const panel=$('lyrics-panel').getBoundingClientRect(),control=$('lyrics-offset-controls').getBoundingClientRect();return {left:control.left>=panel.left,right:control.right<=panel.right,visible:control.width>0&&control.bottom<=panel.bottom,width:panel.width}})()");
    assert.ok(bounds.left&&bounds.right&&bounds.visible,JSON.stringify(bounds));
    check('mobile viewport keeps complete offset controls inside lyric panel; no JS exceptions');
  }finally{
    if(a)await a.close();if(b)await b.close();await stopServer();
    fs.writeFileSync(path.join(__dirname,'artifacts/global-offset-results.json'),JSON.stringify({
      method:'Real Windows headless Edge + Chrome; fixture playback/DeepL; actual settings file and process restart; no real Mac/iOS device',settings_test_path:settings,results},null,2));
  }
})().catch(error=>{console.error(error);process.exitCode=1});
