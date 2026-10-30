/* Real Windows Edge/Chrome via CDP, with a loopback fixture. No npm packages. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {spawn} = require('node:child_process');
const base = 'http://127.0.0.1:18765';
const output = path.resolve(__dirname, 'artifacts');
fs.mkdirSync(output, {recursive:true});
const sleep = ms => new Promise(r => setTimeout(r,ms));
const results = [];

async function fixture(data){
  await fetch(base+'/__test',{method:'POST',body:JSON.stringify(data)});
}
async function run(name, executable, zoomFactor=1){
  const tag=name+' '+Date.now();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(),'remote-lyrics-'+name+'-'));
  if(zoomFactor!==1){
    // Real browser zoom prefs, same HostZoomMap used by the browser zoom UI.
    // https://chromium.googlesource.com/chromium/src/+/main/chrome/browser/ui/zoom/chrome_zoom_level_prefs.cc
    fs.mkdirSync(path.join(profile,'Default'));
    fs.writeFileSync(path.join(profile,'Default','Preferences'),JSON.stringify({
      partition:{default_zoom_level:{x:Math.log(zoomFactor)/Math.log(1.2)}}}));
  }
  const browser = spawn(executable,['--headless=new','--disable-gpu','--no-first-run',
    '--disable-background-timer-throttling','--disable-renderer-backgrounding','--disable-backgrounding-occluded-windows',
    '--no-default-browser-check','--window-size=1440,1000','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],
    {windowsHide:true,stdio:'ignore'});
  let socket;
  try {
    for(let i=0;i<100&&!fs.existsSync(path.join(profile,'DevToolsActivePort'));i++)await sleep(100);
    const port = fs.readFileSync(path.join(profile,'DevToolsActivePort'),'utf8').split('\n')[0];
    const targets = await(await fetch('http://127.0.0.1:'+port+'/json/list')).json();
    const pageTarget=targets.find(t=>t.type==='page');
    socket = new WebSocket(pageTarget.webSocketDebuggerUrl);
    await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
    let id=0;const pending=new Map(),exceptions=[];
    socket.onmessage = event=>{
      const msg=JSON.parse(event.data);
      if(msg.method==='Runtime.exceptionThrown')exceptions.push(msg.params.exceptionDetails.text+' '+(msg.params.exceptionDetails.exception?.description||''));
      if(msg.id){const callback=pending.get(msg.id);pending.delete(msg.id);
        if(!callback)return;
        if(msg.error)callback.reject(new Error(JSON.stringify(msg.error)));else callback.resolve(msg.result)}
    };
    function cdp(method,params={}){return new Promise((resolve,reject)=>{
      const request=++id,timer=setTimeout(()=>{pending.delete(request);reject(new Error('CDP timeout: '+method))},30000);
      pending.set(request,{resolve:value=>{clearTimeout(timer);resolve(value)},reject:error=>{clearTimeout(timer);reject(error)}});
      socket.send(JSON.stringify({id:request,method,params}));
    })}
    async function evaluate(expression){
      const r=await cdp('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});
      if(r.exceptionDetails)throw new Error(r.exceptionDetails.exception?.description||r.exceptionDetails.text);
      return r.result.value;
    }
    async function until(expression,timeout=6000){
      const end=Date.now()+timeout;
      while(Date.now()<end){if(await evaluate(expression))return;await sleep(80)}
      throw new Error('Timed out: '+expression);
    }
    function check(label,details={}){results.push({browser:name,check:label,passed:true,...details});console.log(name+': '+label)}
    await cdp('Runtime.enable');await cdp('Page.enable');
    // Headless Edge can randomly report a busy page as hidden, which pauses all
    // animations mid-test. Model a page the user is looking at.
    await cdp('Emulation.setFocusEmulationEnabled',{enabled:true});
    // Animation is decoration: behaviour checks run with reduced motion; the
    // motion block near the end re-enables it and checks the animated paths.
    await cdp('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]});
    check('browser version',await cdp('Browser.getVersion'));
    await fixture({state:{ok:true,title:'Test Song',playing:false,pos:10,dur:180},mode:'synced',delay:0,clear_calls:true,
      translation_case:'',translation_delay:0,translation_error:'',translation_model_available:true,reset_translation_errors:true});
    await cdp('Page.navigate',{url:base});
    await until("typeof toggleLyrics==='function'&&lyricTrack!==null");
    await evaluate("if(!lyricsVisible)toggleLyrics()");
    await until("lyricLines.length===60&&lyricActive===3");
    check('timestamp positioning, Chinese + English text');
    assert.equal(await evaluate("document.querySelector('#view-now main').children.length"),5);
    check('main retains five direct children and original IDs');
    // #sbtn is the catalog search entry since the catalog UI; retired library functions stay absent.
    assert.ok(await evaluate("$('hbtn').hidden&&!$('sbtn').hidden&&!$('sbtn').disabled&&$('sbtn').getAttribute('aria-label')==='搜索'"));
    assert.ok(await evaluate("typeof openPls==='undefined'&&typeof doSearch==='undefined'&&typeof openSrc==='undefined'"));
    const header=await evaluate("(()=>{const a=$('lyric-toggle').getBoundingClientRect(),b=$('qbtn').getBoundingClientRect(),s=$('lyrics-scroll');return {centers:Math.abs(a.top+a.height/2-b.top-b.height/2),toggleHeight:a.height,queueHeight:b.height,scrollbarWidth:getComputedStyle(s).scrollbarWidth,scrollbarColor:getComputedStyle(s).scrollbarColor,track:getComputedStyle(s,'::-webkit-scrollbar-track').backgroundColor,webkitWidth:getComputedStyle(s,'::-webkit-scrollbar').width}})()");
    assert.ok(header.centers<1);assert.equal(header.toggleHeight,44);assert.equal(header.queueHeight,44);
    assert.equal(header.scrollbarWidth,'thin');assert.ok(header.scrollbarColor.includes('rgba(0, 0, 0, 0)'));
    assert.equal(header.track,'rgba(0, 0, 0, 0)');assert.equal(header.webkitWidth,'6px');
    check('retired library entries absent, search entry present, aligned header, thin transparent scrollbar',header);
    assert.equal(await evaluate('translationEnabled'),false);
    assert.ok(!(await(await fetch(base+'/__test')).json()).calls.includes('/lyrics/translation'));
    check('translation defaults off and makes no translation requests');
    await evaluate("window.__translationStarts=[];window.__originalFetch=window.fetch;window.fetch=(input,options)=>{if(typeof input==='string'&&input.startsWith('/lyrics/translation'))window.__translationStarts.push({input,enabled:translationEnabled});return window.__originalFetch(input,options)}");
    await fixture({state:{title:'Translation '+tag,pos:10},translation_case:'english',translation_delay:.25});
    await evaluate('poll()');await until("rawLyrics&&rawLyrics.record.trackName==='Translation '+"+JSON.stringify(tag));
    await evaluate('toggleTranslation()');
    assert.ok(await evaluate("lyricNodes.length===60&&$('lyrics-scroll').textContent.includes('English lyric')"));
    await until("$('lyrics-scroll').querySelectorAll('.lyric-translation').length===60",10000);
    assert.ok(await evaluate("$('translation-status').textContent.includes('Hy-MT2 本地翻译')"));
    assert.ok(await evaluate("lyricNodes[lyricActive].classList.contains('current')&&lyricNodes[lyricActive].querySelector('.lyric-original')&&lyricNodes[lyricActive].querySelector('.lyric-translation')"));
    check('background pretranslation, exact 60-line mapping, original and translation share active unit');
    await fixture({state:{pos:18,playing:true}});await evaluate('poll()');await sleep(600);
    assert.ok(await evaluate('playbackPosition()>18.4'));
    await fixture({state:{pos:22,playing:false}});await evaluate('poll()');await sleep(300);
    assert.equal(await evaluate('playbackPosition()'),22);
    assert.equal(await evaluate('seekTo(90);lyricActive'),30);
    assert.equal(await evaluate('seekTo(3);lyricActive'),1);
    await evaluate('seekTo(10)');await fixture({state:{pos:10}});
    check('pause/resume and forward/backward seek keep bilingual units synchronized');
    // The translation switch lives in the 调整 popover.
    await evaluate("toggleLyricSheet(true);$('translation-toggle').focus()");
    await cdp('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',text:'\r',windowsVirtualKeyCode:13});
    await cdp('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
    assert.equal(await evaluate('translationEnabled'),false);
    assert.ok(await evaluate("$('translation-toggle').matches(':focus-visible')&&getComputedStyle($('translation-toggle')).outlineStyle==='solid'"));
    await evaluate('toggleTranslation();toggleLyricSheet(false)');await until("document.querySelectorAll('.lyric-translation').length===60");
    check('standard translation button supports Enter and visible keyboard focus');
    const beforeToggle=(await(await fetch(base+'/__test')).json()).model_calls.length;
    const rawTexts=await evaluate('rawLyrics.lines.map(r=>r.text)');
    await evaluate('toggleTranslation()');
    assert.equal(await evaluate("document.querySelectorAll('.lyric-translation').length"),0);
    assert.deepEqual(await evaluate('lyricNodes.map(n=>n.textContent)'),rawTexts);
    await evaluate('toggleTranslation()');await until("document.querySelectorAll('.lyric-translation').length===60");
    assert.equal((await(await fetch(base+'/__test')).json()).model_calls.length,beforeToggle);
    check('off immediately restores originals; re-enable reuses current task/cache');
    if(zoomFactor!==1){
      const window=await cdp('Browser.getWindowForTarget');
      for(const [width,height] of [[1440,1000],[1000,900],[1800,1200]]){
        await cdp('Browser.setWindowBounds',{windowId:window.windowId,bounds:{width,height,windowState:'normal'}});
        await sleep(250);
        const metrics=await evaluate("(()=>{const p=$('lyrics-panel').getBoundingClientRect(),a=$('art').getBoundingClientRect(),c=$('ctrl').getBoundingClientRect(),v=$('vol').getBoundingClientRect();return {dpr:devicePixelRatio,innerWidth,innerHeight,panelRight:p.right,panelBottom:p.bottom,controlLeft:c.left,controlTop:c.top,volumeBottom:v.bottom,delta:Math.abs(p.width-a.width)+Math.abs(p.top-a.top)}})()");
        assert.ok(Math.abs(metrics.dpr-zoomFactor)<.01,JSON.stringify(metrics));
        // Native window frames/sidebar reduce the physical content area.
        assert.ok(metrics.innerWidth<=width/zoomFactor+1&&metrics.innerWidth>width/zoomFactor-40,JSON.stringify(metrics));
        assert.ok(metrics.volumeBottom<=metrics.innerHeight+1,JSON.stringify(metrics));
        assert.ok(metrics.panelBottom<=metrics.controlTop+1||metrics.panelRight<=metrics.controlLeft+1,JSON.stringify(metrics));
        assert.ok(metrics.delta<2,JSON.stringify(metrics));
        await evaluate('pauseLyricsFollow()');
        const follow=await evaluate("(()=>{const p=$('lyrics-panel').getBoundingClientRect(),b=$('lyrics-follow').getBoundingClientRect();return {bottom:b.bottom,panelBottom:p.bottom,panelHeight:p.height,panelWidth:p.width,scrollHeight:$('lyrics-scroll').clientHeight,children:[...$('lyrics-panel').children].map(n=>({id:n.id,height:n.getBoundingClientRect().height})),offset:$('lyrics-offset-controls').getBoundingClientRect().width}})()");
        assert.ok(follow.bottom<=follow.panelBottom+1,JSON.stringify(follow));
        await evaluate('resumeLyrics()');
        check('actual window resize with native browser zoom '+zoomFactor*100+'% '+width+'x'+height,metrics);
      }
      const shot=await cdp('Page.captureScreenshot',{format:'png'});
      fs.writeFileSync(path.join(output,name+'.png'),Buffer.from(shot.data,'base64'));
      assert.deepEqual(exceptions,[]);await cdp('Browser.close');return;
    }

    for(const [width,height] of [[1440,900],[900,650],[600,800],[390,844],[320,568],[844,390],[667,375],[1280,360],[320,480]]){
      await cdp('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:width<500});
      await until("Math.abs($('lyrics-panel').getBoundingClientRect().width-$('art').getBoundingClientRect().width)<2&&Math.abs($('lyrics-panel').getBoundingClientRect().height-$('art').getBoundingClientRect().height)<2");
      const bounds=await evaluate(`(()=>{const rect=id=>{const r=$(id).getBoundingClientRect();return {left:r.left,top:r.top,right:r.right,bottom:r.bottom,width:r.width,height:r.height}};
        return {panel:rect('lyrics-panel'),art:rect('art'),ctrl:rect('ctrl'),vol:rect('vol'),scroll:rect('lyrics-scroll'),toggle:rect('lyric-toggle'),
        innerWidth,innerHeight,overflow:document.documentElement.scrollWidth>innerWidth}})()`);
      assert.ok(bounds.scroll.height>15,JSON.stringify(bounds));
      assert.ok(bounds.panel.bottom<=bounds.ctrl.top+1||bounds.panel.right<=bounds.ctrl.left+1,JSON.stringify(bounds));
      assert.ok(bounds.vol.bottom<=height+1,JSON.stringify(bounds));
      assert.ok(bounds.toggle.left>=0&&bounds.toggle.right<=width,JSON.stringify(bounds));
      assert.equal(bounds.overflow,false);
      assert.ok(Math.abs(bounds.art.width-bounds.panel.width)<2);
      await evaluate('pauseLyricsFollow()');
      const followBounds=await evaluate("(()=>{const b=$('lyrics-follow').getBoundingClientRect(),p=$('lyrics-panel').getBoundingClientRect(),s=$('lyrics-scroll').getBoundingClientRect();return {buttonBottom:b.bottom,panelBottom:p.bottom,scrollHeight:s.height}})()");
      assert.ok(followBounds.buttonBottom<=followBounds.panelBottom+1,JSON.stringify(followBounds));
      assert.ok(followBounds.scrollHeight>0,JSON.stringify(followBounds));
      await evaluate('resumeLyrics()');
      check('responsive layout '+width+'x'+height,bounds);
      if(width===1440||width===390){const shot=await cdp('Page.captureScreenshot',{format:'png'});
        fs.writeFileSync(path.join(output,name+'-'+width+'.png'),Buffer.from(shot.data,'base64'))}
    }
    await cdp('Emulation.setDeviceMetricsOverride',{width:600,height:700,deviceScaleFactor:2,mobile:false});
    await evaluate("document.body.style.zoom='1.5';scheduleLyricsLayout()");await sleep(200);
    const zoomBounds=await evaluate("(()=>{const p=$('lyrics-panel').getBoundingClientRect(),a=$('art').getBoundingClientRect();return {p:{x:p.x,y:p.y,width:p.width,height:p.height},a:{x:a.x,y:a.y,width:a.width,height:a.height}}})()");
    for(const key of ['x','y','width','height'])assert.ok(Math.abs(zoomBounds.p[key]-zoomBounds.a[key])<2,JSON.stringify(zoomBounds));
    // CSS zoom is a stress check, not a substitute for a browser UI zoom test.
    check('CSS zoom stress measurement (not browser UI zoom)',zoomBounds);
    await evaluate("document.body.style.zoom='';scheduleLyricsLayout()");await sleep(150);

    // Preserve the manually visible row when translation changes line heights.
    await evaluate("pauseLyricsFollow();$('lyrics-scroll').scrollTop=1200");
    const anchor=await evaluate("(()=>{const s=$('lyrics-scroll'),n=lyricNodes.find(n=>n.getBoundingClientRect().bottom>s.getBoundingClientRect().top);return {id:n.dataset.lineId,y:n.getBoundingClientRect().top}})()");
    await evaluate('toggleTranslation()');
    const anchored=await evaluate("document.querySelector('[data-line-id='+"+JSON.stringify(anchor.id)+"+']').getBoundingClientRect().top");
    assert.ok(Math.abs(anchored-anchor.y)<3,JSON.stringify({anchor,anchored}));
    await evaluate('toggleTranslation()');await until("document.querySelectorAll('.lyric-translation').length===60");
    assert.ok(await evaluate('manualUntil>Date.now()'));
    await evaluate('resumeLyrics();seekTo(90)');
    assert.equal(await evaluate('lyricActive'),30);
    assert.ok(await evaluate('lyricNodes[30].querySelector(".lyric-translation")!==null'));
    check('manual row anchoring through toggles, resume-follow and seek with dual-line units');
    const wheelRect=await evaluate("(()=>{const r=$('lyrics-scroll').getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2}})()");
    await cdp('Input.synthesizeScrollGesture',{...wheelRect,yDistance:100,gestureSourceType:'mouse',speed:400});
    await until('manualUntil>Date.now()');check('browser wheel input suspends following (trackpad-style wheel path)');
    await evaluate('resumeLyrics()');

    for(const [testCase,hasTranslation] of [['traditional',false],['simplified',false],['mixed',true],['japanese',true],['paired',false]]){
      await fixture({state:{title:'Case '+testCase+' '+tag},translation_case:testCase,translation_delay:0});
      await evaluate('poll()');await until("rawLyrics&&rawLyrics.record.trackName==="+JSON.stringify('Case '+testCase+' '+tag));
      await until("!translationRequest&&!translationTimer&&derivedRows.size===60",10000);
      assert.equal(await evaluate("document.querySelectorAll('.lyric-translation').length>0"),hasTranslation);
      if(testCase==='traditional')assert.ok(await evaluate("lyricNodes[0].textContent.includes('风')&&!lyricNodes[0].textContent.includes('風')"));
      if(testCase==='japanese')assert.equal(await evaluate("lyricNodes[2].querySelector('.lyric-original').textContent"),'愛');
      if(testCase==='paired')assert.equal(await evaluate('lyricNodes[0].textContent'),'I will remember you\n我会永远记住你');
      const original=await evaluate('rawLyrics.lines.map(r=>r.text)');await evaluate('toggleTranslation()');
      assert.deepEqual(await evaluate('lyricNodes.map(n=>n.textContent)'),original);
      await evaluate('toggleTranslation()');await until('derivedRows.size===60&&!translationRequest&&!translationTimer');
      check('display and exact original restoration: '+testCase);
    }
    await fixture({state:{title:'Plain translation '+tag},mode:'plain',translation_case:'english'});
    await evaluate('poll()');await until("rawLyrics&&rawLyrics.state==='plain'&&document.querySelectorAll('.lyric-translation').length===60");
    assert.equal(await evaluate('lyricLines.length'),0);assert.equal(await evaluate('lyricActive'),-1);
    check('plain lyric translation without fabricated timestamps');
    await fixture({state:{title:'No Model mixed '+tag},mode:'synced',translation_case:'mixed',translation_model_available:false});
    await evaluate('poll()');await until("$('translation-status').textContent.includes('本地 Ollama 未运行')");
    assert.ok(await evaluate("lyricNodes[0].textContent.includes('今天的风')"));
    assert.equal(await evaluate("document.querySelectorAll('.lyric-translation').length"),0);
    check('unavailable local model notice with local Chinese conversion still working');
    await fixture({translation_model_available:true,state:{title:'Delayed translation '+tag},translation_case:'english',translation_delay:1});
    await evaluate('poll()');await until("rawLyrics&&rawLyrics.record.trackName==="+JSON.stringify('Delayed translation '+tag));
    await until('translationTimer!==0');
    await evaluate('toggleTranslation()');
    const stoppedCalls=await evaluate('window.__translationStarts.length');
    await sleep(1300);await evaluate('poll();tick()');
    assert.equal(await evaluate("document.querySelectorAll('.lyric-translation').length"),0);
    assert.equal(await evaluate('window.__translationStarts.length'),stoppedCalls);
    assert.ok(await evaluate('window.__translationStarts.every(call=>call.enabled)'));
    check('turning off stops starting/polling tasks and ignores already-sent completion');
    await fixture({state:{title:'Old derived '+tag},translation_delay:1});await evaluate('toggleTranslation();poll()');
    await until("rawLyrics&&rawLyrics.record.trackName==="+JSON.stringify('Old derived '+tag));
    await until('translationTimer!==0');
    await fixture({state:{title:'New derived '+tag},translation_delay:0});await evaluate('poll()');
    await until("rawLyrics&&rawLyrics.record.trackName==="+JSON.stringify('New derived '+tag)+"&&document.querySelectorAll('.lyric-translation').length===60",10000);
    await sleep(2300);
    assert.ok(await evaluate("!$('lyrics-scroll').textContent.includes('Old derived')&&$('lyrics-scroll').textContent.includes('New derived')"));
    check('rapid track change ignores old derived response while backend may cache it');
    await cdp('Page.reload');await until("translationEnabled&&document.querySelectorAll('.lyric-translation').length===60");
    check('translation preference survives reload');
    await evaluate("toggleTranslation();resumeLyrics()");
    await fixture({state:{title:'Test Song',pos:10,playing:false},mode:'synced',translation_case:'',translation_delay:0});
    await evaluate('poll()');await until("lyricLines.length===60&&lyricLines[0].text.startsWith('Test Song:')");

    await evaluate("$('lyrics-scroll').dispatchEvent(new WheelEvent('wheel',{deltaY:100}));$('lyrics-scroll').scrollTop=0");
    assert.equal(await evaluate("$('lyrics-follow').hidden"),false);
    await evaluate('syncLyrics(100)');
    assert.equal(await evaluate("$('lyrics-scroll').scrollTop"),0);
    const following=await evaluate("syncLyrics(100);resumeLyrics();({top:$('lyrics-scroll').scrollTop,hidden:$('lyrics-follow').hidden,manualUntil,lyricActive,view,lyricsVisible,rows:lyricNodes.length,height:$('lyrics-scroll').clientHeight,scrollHeight:$('lyrics-scroll').scrollHeight,nodeTop:lyricNodes[lyricActive].getBoundingClientRect().top,panelTop:$('lyrics-panel').getBoundingClientRect().top,innerWidth,innerHeight,art:$('art').getBoundingClientRect().toJSON(),main:document.querySelector('main').getBoundingClientRect().toJSON(),mainHeight:getComputedStyle(document.querySelector('main')).height,mainMaxHeight:getComputedStyle(document.querySelector('main')).maxHeight,bodyHeight:getComputedStyle(document.body).height,panelPosition:getComputedStyle($('lyrics-panel')).position})");
    assert.ok(following.top>0&&following.hidden,JSON.stringify(following));
    await evaluate("$('lyrics-scroll').dispatchEvent(new Event('touchstart'));manualUntil=Date.now()-1;syncLyrics(80)");
    assert.equal(await evaluate('manualUntil'),0);
    check('mouse/touch pause follow, return button, timed resume');
    await evaluate("$('lyrics-scroll').dispatchEvent(new KeyboardEvent('keydown',{key:'PageUp'}))");
    assert.ok(await evaluate('manualUntil>Date.now()'));
    await evaluate("resumeLyrics();$('qbtn').focus()");
    await cdp('Input.dispatchKeyEvent',{type:'keyDown',key:'Tab',code:'Tab',modifiers:8,windowsVirtualKeyCode:9});
    await cdp('Input.dispatchKeyEvent',{type:'keyUp',key:'Tab',code:'Tab',modifiers:8,windowsVirtualKeyCode:9});
    assert.ok(await evaluate("$('lyric-toggle').matches(':focus-visible')&&getComputedStyle($('lyric-toggle')).outlineStyle==='solid'"));
    await cdp('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',text:'\r',unmodifiedText:'\r',windowsVirtualKeyCode:13});
    await cdp('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
    await until('!lyricsVisible');
    await evaluate('toggleLyrics()');
    check('keyboard toggle, visible focus, keyboard lyric scrolling');

    await fixture({state:{pos:18,playing:true}});await evaluate('poll()');
    await sleep(1100);assert.ok(await evaluate('playbackPosition()>18.9'));
    await fixture({state:{pos:22,playing:false}});await evaluate('poll()');
    await sleep(550);assert.equal(await evaluate('playbackPosition()'),22);
    await evaluate('seekTo(90)');assert.equal(await evaluate('lyricActive'),30);
    await evaluate('seekTo(3)');assert.equal(await evaluate('lyricActive'),1);
    await evaluate('scrub(45)');assert.equal(await evaluate('lyricActive'),15);
    await evaluate('seekTo(45)');
    check('pause/resume, forward/back seek, scrub preview');

    const oldCalls=(await(await fetch(base+'/__test')).json()).calls.filter(c=>c.startsWith('/lyrics?')).length;
    await evaluate('poll();tick()');await sleep(100);await evaluate('poll();tick()');
    const newCalls=(await(await fetch(base+'/__test')).json()).calls.filter(c=>c.startsWith('/lyrics?')).length;
    assert.equal(oldCalls,newCalls);check('status polling never re-requests lyrics');
    await cdp('Page.reload');await until('lyricsVisible&&lyricLines.length===60');
    check('localStorage display preference after reload');

    await fixture({state:{title:'Slow Old Song'},mode:'synced',delay:1.2});await evaluate('poll()');
    await until("$('lyrics-status').textContent==='加载中…'");
    assert.equal(await evaluate("$('lyrics-scroll').children.length"),0);
    await fixture({state:{title:'New Song'},delay:0});await evaluate('poll()');
    await until("lyricLines.length===60&&lyricLines[0].text.startsWith('New Song:')");
    await sleep(1400);assert.ok(await evaluate("lyricLines[0].text.startsWith('New Song:')"));
    await evaluate("(async()=>{const pendingCommand=c('next');window.__cleared=$('lyrics-scroll').children.length===0;await pendingCommand})()");
    assert.equal(await evaluate('window.__cleared'),true);
    await until("lyricLines.length===60&&lyricLines[0].text.startsWith('New Song next:')");
    check('rapid track switch clears immediately; old response cannot overwrite');

    for(const [mode,message] of [['plain','普通歌词'],['not_found','暂无匹配歌词'],['instrumental','纯音乐'],['error','歌词请求失败']]){
      await fixture({state:{title:mode},mode,delay:0});await evaluate('poll()');
      await until("$('lyrics-status').textContent.includes("+JSON.stringify(message)+")");
      if(mode==='plain'){assert.equal(await evaluate("$('lyrics-scroll').querySelectorAll('.lyrics-plain').length"),1);
        assert.ok(await evaluate("$('lyrics-scroll').scrollHeight>$('lyrics-scroll').clientHeight"))}
      assert.equal(await evaluate("$('lyrics-retry').hidden"),mode!=='error');
      check('distinct state '+mode);
    }
    await fixture({mode:'synced'});await evaluate('requestLyrics()');await until('lyricLines.length===60');
    check('retry after failure');
    await fixture({state:{title:'Approximate '+tag},match_quality:'approximate'});await evaluate('poll()');
    await until("lyricLines.length===60&&$('lyrics-status').textContent==='逐行同步 · 近似匹配'");
    assert.ok(await evaluate("$('lyrics-tune').classList.contains('attention')"));
    assert.ok(await evaluate("[...$('lyrics-version').options].some(o=>o.value==='QM-fixture'&&o.textContent.startsWith('近似 · QQ音乐'))"));
    assert.ok(await evaluate("[...$('lyrics-version').options].some(o=>o.value==='NE-fixture'&&o.textContent.startsWith('网易云音乐'))"));
    await fixture({state:{title:'Exact '+tag},match_quality:''});await evaluate('poll()');
    await until("lyricLines.length===60&&$('lyrics-status').textContent==='逐行同步'");
    assert.ok(await evaluate("!$('lyrics-tune').classList.contains('attention')"));
    check('approximate match labeled in status, version list and 调整 dot');
    await cdp('Network.enable');
    await cdp('Network.emulateNetworkConditions',{offline:true,latency:0,downloadThroughput:0,uploadThroughput:0});
    await evaluate("clearLyrics('加载中…');requestLyrics()");await until("!$('lyrics-retry').hidden");
    assert.ok(await evaluate("$('lyrics-status').textContent.includes('请求失败')"));
    await cdp('Network.emulateNetworkConditions',{offline:false,latency:0,downloadThroughput:-1,uploadThroughput:-1});
    await evaluate('requestLyrics()');await until('lyricLines.length===60');check('browser offline and reconnect');
    if(name==='edge'&&!process.argv.includes('--skip-timeout')){
      await fixture({state:{title:'Source timeout'},mode:'synced',delay:23});await evaluate('poll()');
      await until("$('lyrics-status').textContent==='加载中…'");
      await until("$('lyrics-status').textContent.includes('请求失败')",22500);
      assert.equal(await evaluate('lyricLines.length'),0);
      await fixture({state:{title:'After timeout'},delay:0});await evaluate('poll()');await until('lyricLines.length===60');
      check('client 21-second timeout, later response ignored')
    }

    // Only a stated model of the native injector; this is not an iOS test.
    await evaluate(`(()=>{const art=$('art'),stage=document.createElement('div');stage.id='remote-art-stage';
      art.parentNode.insertBefore(stage,art);stage.appendChild(art);
      stage.style.cssText='min-height:0;min-width:0;width:100%;height:100%';
      const style=document.createElement('style');style.textContent='#view-now main{display:grid!important;grid-template-columns:1fr!important;grid-template-rows:minmax(0,1fr) auto auto auto auto!important;width:88vw!important}#view-now main>*{grid-column:auto!important;grid-row:auto!important}';document.head.appendChild(style)})()`);
    await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:2,mobile:true});
    await until("Math.abs($('lyrics-panel').getBoundingClientRect().width-$('remote-art-stage').getBoundingClientRect().width)<2&&Math.abs($('lyrics-panel').getBoundingClientRect().height-$('remote-art-stage').getBoundingClientRect().height)<2");
    const injected=await evaluate("(()=>{const a=$('remote-art-stage').getBoundingClientRect(),b=$('lyrics-panel').getBoundingClientRect();return {children:document.querySelector('#view-now main').children.length,view,lyricsVisible,layoutFrame,a:{top:a.top,width:a.width,height:a.height},b:{top:b.top,width:b.width,height:b.height},delta:Math.abs(a.top-b.top)+Math.abs(a.height-b.height)+Math.abs(a.width-b.width)}})()");
    assert.equal(injected.children,5);assert.ok(injected.delta<2,JSON.stringify(injected));
    check('modeled WKWebView wrapping/portrait grid compatibility (not real iOS)',injected);
    await evaluate("document.querySelector('style:last-child').textContent='#view-now main{display:grid!important;grid-template-columns:1fr 1fr!important;grid-template-rows:repeat(4,auto)!important;width:90vw!important}#remote-art-stage{grid-column:1!important;grid-row:1 / 5!important}#view-now main>div:nth-child(n+2){grid-column:2!important;grid-row:auto!important}'");
    await cdp('Emulation.setDeviceMetricsOverride',{width:844,height:390,deviceScaleFactor:2,mobile:true});
    await until("Math.abs($('lyrics-panel').getBoundingClientRect().width-$('remote-art-stage').getBoundingClientRect().width)<2");
    assert.ok(await evaluate("Math.abs($('lyrics-panel').getBoundingClientRect().width-$('remote-art-stage').getBoundingClientRect().width)<2"));
    check('modeled WKWebView landscape grid compatibility (not real iOS)');

    await evaluate("$('qbtn').click()");await until("$('que-list').querySelectorAll('.row').length===1");
    assert.ok(await evaluate("view==='que'&&!$('hbtn').hidden"));
    await evaluate("$('hbtn').click();c('toggle');vset(35)");await sleep(200);
    assert.ok(await evaluate("view==='now'&&$('hbtn').hidden"));
    for(const removed of ['pls','trs','src']){
      await evaluate('show('+JSON.stringify(removed)+')');assert.equal(await evaluate('view'),'now');
    }
    await sleep(150);
    await until("Math.abs($('lyrics-panel').getBoundingClientRect().top-($('remote-art-stage')||$('art')).getBoundingClientRect().top)<2");
    assert.ok(await evaluate("Math.abs($('lyrics-panel').getBoundingClientRect().top-($('remote-art-stage')||$('art')).getBoundingClientRect().top)<2"));
    check('overlay alignment after returning from other views');
    await until("fetch('/__test').then(r=>r.json()).then(r=>r.calls.includes('/cmd/vol/35'))");
    const allCalls=(await(await fetch(base+'/__test')).json()).calls;
    for(const route of ['/queue','/cmd/toggle','/cmd/vol/35'])assert.ok(allCalls.includes(route),route);
    assert.ok(!allCalls.some(route=>/^\/(playlists|tracks|search|splay|play)(?:[/?]|$)/.test(route)));
    check('queue entry/back navigation, transport/volume, no retired feature requests');
    await fixture({state:{title:'Version '+tag,pos:10,playing:false},mode:'synced',delay:0,
      translation_case:'english',translation_delay:.25,translation_error:'',translation_model_available:true});
    await evaluate('poll()');await until("rawLyrics&&rawLyrics.record.trackName==='Version '+"+JSON.stringify(tag));
    await evaluate('if(!translationEnabled)toggleTranslation()');
    await until("document.querySelectorAll('.lyric-translation').length===60");
    const firstVersion=await evaluate('({id:rawLyrics.lyrics_id,hash:rawLyrics.content_hash})');
    await fixture({delay:.4});
    await evaluate("$('lyrics-version').value='NE-fixture';changeLyricVersion()");
    assert.equal(await evaluate('derivedRows.size'),0);
    await until("rawLyrics&&rawLyrics.source==='网易云音乐'");
    await until("document.querySelectorAll('.lyric-translation').length===60");
    assert.notEqual(await evaluate('rawLyrics.content_hash'),firstVersion.hash);
    assert.notEqual(await evaluate('rawLyrics.lyrics_id'),firstVersion.id);
    assert.ok(await evaluate("lyricNodes.every(n=>n.querySelector('.lyric-original').textContent.startsWith('Alternate version'))"));
    assert.ok(await evaluate("$('lyrics-source-label').textContent.includes('网易云音乐')"));
    assert.ok(await evaluate("lyricNodes.every(n=>n.querySelector('.lyric-translation').textContent.includes('Alternate version'))"));
    await evaluate('adjustLyricOffset(.1);adjustLyricOffset(.1)');
    assert.equal(await evaluate('lyricOffset'),.2);
    await until('!offsetPending&&!offsetSaving&&offsetConfirmed.lyric_offset===.2');
    await fixture({delay:0});
    await cdp('Page.reload');await until("rawLyrics&&rawLyrics.source==='网易云音乐'");
    assert.equal(await evaluate('lyricOffset'),.2);
    assert.equal(await evaluate('lyricChoice'),'NE-fixture');
    await evaluate('adjustLyricOffset(0,true)');
    await until('!offsetPending&&!offsetSaving&&offsetConfirmed.lyric_offset===0');
    check('manual source switch invalidates old mapping, translates new content and persists choice/offset');
    // Controlled comparison using exactly the same displayed song and stamps;
    // isolate scheduling delay from the playback clock and upstream timestamps.
    const latency=await evaluate(`(async()=>{
      const saved={lines:lyricLines,playing,pos,posTs,dur,busy:pollBusy};
      pollBusy=true;clearInterval(lyricTickTimer);lyricTickTimer=0;
      lyricLines=Array.from({length:9},(_,i)=>({time:.117+i*.31,text:rawLyrics.lines[i].text}));
      async function measure(period){
        pos=0;dur=180;playing=true;posTs=performance.now();lyricActive=-1;manualUntil=0;lyricOffset=0;
        const delays=[],changes=[],observer=new MutationObserver(()=>{
          const active=lyricActive;if(active>=0&&!changes.includes(active)){
            changes.push(active);delays.push((playbackPosition()-lyricLines[active].time)*1000)}});
        observer.observe($('lyrics-scroll'),{subtree:true,attributes:true,attributeFilter:['aria-current']});
        const timer=setInterval(()=>syncLyrics(playbackPosition()),period);
        await new Promise(resolve=>setTimeout(resolve,3100));clearInterval(timer);observer.disconnect();
        return {period_ms:period,changes:delays.length,mean_ms:delays.reduce((a,b)=>a+b,0)/delays.length,max_ms:Math.max(...delays)};
      }
      const before=await measure(500),after=await measure(75);
      lyricLines=saved.lines;pos=saved.pos;posTs=performance.now();playing=saved.playing;dur=saved.dur;
      pollBusy=saved.busy;startLyricClock();clockRecalibrate=true;syncLyrics(playbackPosition(),true);
      return {before,after,clock_bias_ms:0,timestamp_bias_ms:0,method:'same song fixture, controlled local clock/stamps; scheduling delay only'}
    })()`);
    assert.ok(latency.after.max_ms<150,JSON.stringify(latency));
    assert.ok(latency.after.mean_ms<latency.before.mean_ms,JSON.stringify(latency));
    check('same-song controlled before/after highlight latency',latency);
    const steadyCalls=(await(await fetch(base+'/__test')).json()).calls;
    await sleep(700);
    const laterCalls=(await(await fetch(base+'/__test')).json()).calls;
    assert.equal(laterCalls.filter(p=>p.startsWith('/lyrics?')).length,steadyCalls.filter(p=>p.startsWith('/lyrics?')).length);
    check('75ms highlight loop makes no source requests');
    const noDom=await evaluate(`(async()=>{
      playing=false;pos=10;posTs=performance.now();lyricOffset=0;syncLyrics(playbackPosition(),true);
      let mutations=0;const observer=new MutationObserver(changes=>{mutations+=changes.length});
      observer.observe($('lyrics-scroll'),{subtree:true,attributes:true,attributeFilter:['class','aria-current']});
      const timer=lyricTickTimer;for(let i=0;i<5;i++)startLyricClock();
      await new Promise(r=>setTimeout(r,350));observer.disconnect();
      return {mutations,single_timer:timer===lyricTickTimer}
    })()`);
    assert.equal(noDom.mutations,0);assert.equal(noDom.single_timer,true);
    await evaluate('seekTs=0');
    await fixture({state:{pos:10,playing:true,timeline_sample:'fixed-sample',position_precision_ms:1000}});
    await evaluate('clockRecalibrate=true;poll()');await sleep(450);
    const extrapolated=await evaluate('playbackPosition()');
    await evaluate('poll()');
    assert.ok(await evaluate('playbackPosition()')>=extrapolated);
    // Changing a coarse SMTC sample within its 1s precision must not drag the
    // monotonic client clock back to an integer-second position on every poll.
    await fixture({state:{timeline_sample:'next-coarse-sample'}});await evaluate('poll()');
    assert.ok(await evaluate('playbackPosition()')>=extrapolated);
    check('single timer, unchanged lines make no highlight DOM mutations, repeated/coarse samples do not pull clock back');

    // Motion enabled: animated paths must reach exactly the same final state.
    // Runs before the lifecycle freeze below, which can leave a headless page hidden.
    await cdp('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'no-preference'}]});
    await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:2,mobile:true});
    // Re-assert the viewed-page model: animations only advance on a visible page.
    await cdp('Emulation.setFocusEmulationEnabled',{enabled:false});await cdp('Emulation.setFocusEmulationEnabled',{enabled:true});
    await fixture({state:{title:'Motion '+tag,pos:10,playing:false,timeline_sample:''},mode:'synced',delay:0,
      translation_case:'',translation_delay:0,translation_error:''});
    await cdp('Page.reload');await until("lyricsVisible&&lyricLines.length===60&&lyricActive===3&&!reducedMotion.matches");
    // Arriving translations legitimately re-center rows; keep this block about motion only.
    await evaluate('if(translationEnabled)toggleTranslation()');await until("!translationRequest&&document.querySelectorAll('.lyric-translation').length===0");
    await until("document.visibilityState==='visible'");
    await sleep(700);
    const rest="lyricNodes.every(n=>['none','0px','0px 0px'].includes(getComputedStyle(n).translate))";
    const flip=await evaluate(`(()=>{seekTo(24);return {active:lyricActive,top:$('lyrics-scroll').scrollTop,visibility:document.visibilityState,
      flipping:flipUntil>performance.now(),start:getComputedStyle(lyricNodes[lyricActive]).translate}})()`);
    assert.ok(flip.active===8&&flip.top>0&&flip.flipping&&!['none','0px','0px 0px'].includes(flip.start),JSON.stringify(flip));
    await sleep(1400);
    const settled=await evaluate(`(()=>{const s=$('lyrics-scroll'),n=lyricNodes[lyricActive],a=s.getBoundingClientRect(),b=n.getBoundingClientRect();
      return {resting:${rest},offset:Math.abs(b.top+b.height/2-(a.top+s.clientHeight/2)),top:s.scrollTop,visibility:document.visibilityState,
        animations:document.getAnimations().map(x=>x.playState+':'+(x.transitionProperty||x.animationName)).slice(0,6)}})()`);
    assert.ok(settled.resting&&settled.offset<3&&settled.top===flip.top,JSON.stringify({flip,settled}));
    check('motion: scroll position is final immediately; staggered line wave settles centered',{flip,settled});
    const manual=await evaluate(`(()=>{seekTo(90);$('lyrics-scroll').dispatchEvent(new WheelEvent('wheel',{deltaY:40}));
      return {flipping:flipUntil>performance.now(),browsing:$('lyrics-panel').classList.contains('browsing'),resting:${rest}}})()`);
    assert.ok(!manual.flipping&&manual.browsing&&manual.resting,JSON.stringify(manual));
    await evaluate('resumeLyrics()');assert.equal(await evaluate("$('lyrics-panel').classList.contains('browsing')"),false);
    await sleep(1400);assert.ok(await evaluate(rest));
    check('motion: manual scroll cancels the wave instantly; resume animates back');
    await evaluate("$('lyrics-tune').click()");
    assert.ok(await evaluate("!$('lyrics-sheet').hidden&&$('lyrics-tune').getAttribute('aria-expanded')==='true'"));
    await cdp('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
    await cdp('Input.dispatchKeyEvent',{type:'keyUp',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
    assert.ok(await evaluate("$('lyrics-sheet').hidden&&document.activeElement===$('lyrics-tune')&&$('lyrics-tune').getAttribute('aria-expanded')==='false'"));
    await evaluate("$('lyrics-tune').click()");assert.equal(await evaluate("$('lyrics-sheet').hidden"),false);
    await cdp('Input.dispatchMouseEvent',{type:'mousePressed',x:4,y:420,button:'left',clickCount:1});
    await cdp('Input.dispatchMouseEvent',{type:'mouseReleased',x:4,y:420,button:'left',clickCount:1});
    assert.ok(await evaluate("$('lyrics-sheet').hidden"));
    check('settings popover: button, Escape returns focus, outside pointer closes');
    await evaluate('toggleLyrics()');
    await until("(()=>{const a=$('art');return a.offsetWidth>50&&a.offsetWidth===a.offsetHeight})()");
    const square=await evaluate("(()=>{const a=$('art'),m=a.parentElement;return {width:a.offsetWidth,height:a.offsetHeight,main:m.clientWidth}})()");
    assert.ok(square.width<=square.main,JSON.stringify(square));
    await evaluate('toggleLyrics()');
    await until("Math.abs($('lyrics-panel').getBoundingClientRect().height-$('art').getBoundingClientRect().height)<2&&$('art').offsetHeight>$('art').offsetWidth");
    check('artwork is a square while lyrics are hidden and restores the full stage for lyrics',square);
    // backdropSample() reads the pixels of whichever renderer is active (WebGL or the 2D fallback).
    const backdrop=await evaluate(`(()=>{const s=backdropSample(),d=s.data;let low=765,high=0;
      for(let i=0;i<d.length;i+=4){const v=d[i]+d[i+1]+d[i+2];low=Math.min(low,v);high=Math.max(high,v)}
      return {mode:s.mode,source:!!BG.to,width:s.width,height:s.height,range:high-low,theme:document.querySelector('meta[name=theme-color]').content}})()`);
    assert.ok(backdrop.source&&backdrop.range>10&&backdrop.height>backdrop.width&&backdrop.theme.startsWith('rgb('),JSON.stringify(backdrop));
    check('artwork backdrop is drawn at the viewport aspect ratio and tints theme-color',backdrop);
    const icon=await evaluate(`(async()=>{const before=document.body.classList.contains('playing'),command=c('toggle');
      const optimistic=document.body.classList.contains('playing');await command;return {before,optimistic}})()`);
    assert.notEqual(icon.before,icon.optimistic);
    await until("playing!==undefined&&document.body.classList.contains('playing')===playing&&playing===true");
    check('play/pause icon responds immediately and then follows server state',icon);
    await evaluate("$('qbtn').click()");await until("$('que-list').querySelectorAll('.row').length===1");
    assert.equal(await evaluate("$('htitle').textContent"),'待播队列');
    await evaluate("$('hbtn').click()");await until("view==='now'");
    check('localized queue title; animated view switch returns to now playing');
    await cdp('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]});

    // Headless tab visibility is unreliable. Explicitly exercise the handler
    // around a real browser lifecycle freeze; disclose the visibility model.
    const visibilityMethod='modeled visibility events with real CDP lifecycle freeze/resume';
    await evaluate("Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});document.dispatchEvent(new Event('visibilitychange'));startLyricClock()");
    await until('document.hidden&&lyricTickTimer===0');
    await cdp('Page.setWebLifecycleState',{state:'frozen'});await sleep(350);
    await fixture({state:{pos:72,playing:false,timeline_sample:'resume-sample'}});
    await cdp('Page.setWebLifecycleState',{state:'active'});
    await evaluate("Object.defineProperty(document,'hidden',{configurable:true,get:()=>false});document.dispatchEvent(new Event('visibilitychange'))");
    await until('!document.hidden&&lyricTickTimer!==0&&!playing&&Math.abs(playbackPosition()-72)<.01');
    await evaluate('delete document.hidden');
    check('visibility handler stops timer and recalibrates server progress',{method:visibilityMethod});
    assert.deepEqual(exceptions,[]);check('no JavaScript exceptions');
    await cdp('Browser.close');
  } finally {
    if(socket&&socket.readyState===1){
      // Windows browser launchers can outlive spawn's wrapper process. Close
      // the actual isolated browser even when an assertion failed.
      socket.onmessage=()=>{};
      socket.send(JSON.stringify({id:1000000,method:'Browser.close'}));await sleep(150);
    }
    if(socket)socket.close();
    browser.kill();
    // Leave the private profile for diagnostics; never touch the user's profile.
  }
}
(async()=>{
  try{
    if(process.argv.includes('--zoom-only')){
      results.push(...JSON.parse(fs.readFileSync(path.join(output,'browser-results.json'),'utf8')).results.filter(r=>r.browser==='edge'||r.browser==='chrome'));
    }else{
      await run('edge', 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe');
      await run('chrome','C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe');
    }
    await run('edge-zoom200', 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',2);
    await run('chrome-zoom200','C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',2);
  }finally{
    fs.writeFileSync(path.join(output,'browser-results.json'),JSON.stringify({
      method:'Real Windows headless Edge and Chrome via CDP; local fixtures; viewport emulation, native browser zoom via private profile and actual window resize. Mac/iOS are not verified.',results},null,2));
  }
})().catch(error=>{console.error(error);process.exitCode=1});
