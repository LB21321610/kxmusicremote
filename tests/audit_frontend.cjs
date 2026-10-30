/* Production request helpers against an actual stalled loopback HTTP server. */
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict'),http=require('node:http');
const source=fs.readFileSync(path.join(__dirname,'../apple-music-remote.py'),'utf8');
const poll=source.slice(source.indexOf('async function poll(){'),source.indexOf('\nfunction tick(){'));
const helper=source.slice(source.indexOf('async function timedFetch('),source.indexOf('\nfunction controlError('));
const results=[];
const check=name=>{results.push({check:name,passed:true});console.log(name)};
let statusCalls=0;
const server=http.createServer((req,res)=>{
  if(req.url==='/status'&&++statusCalls===1)return;
  if(req.url==='/queue'||req.url==='/queue/play'){
    res.writeHead(200,{'Content-Type':'application/json'});res.write('{"ok":');return;
  }
  if(req.url==='/cmd/toggle'){res.writeHead(204);res.end();return}
  if(req.url==='/error'){res.writeHead(503,{'Content-Type':'application/json'});res.end('{"err":"failure"}');return}
  if(req.url==='/invalid'){res.end('not JSON');return}
  res.writeHead(200,{'Content-Type':'application/json'});res.end('{"ok":false}');
});
(async()=>{
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const base='http://127.0.0.1:'+server.address().port;
  const ctx=vm.createContext({AbortController,Response,performance,
    fetch:(url,opts)=>fetch(base+url,opts),setTimeout:(fn,ms)=>setTimeout(fn,Math.min(ms,150)),clearTimeout,
    pollBusy:false,playbackEpoch:0,playing:false,pos:0,dur:180,posTs:0,scrubbing:false,
    playbackPosition:()=>0,setPlayingClass:()=>{},bgKick:()=>{},setText:()=>{},updateLyricTrack:()=>{},tick:()=>{},
    $:()=>({classList:{remove:()=>{}}})});
  vm.runInContext(helper+'\n'+poll,ctx);
  try {
    const first=ctx.poll();await ctx.poll();await first;
    assert.equal(statusCalls,1);assert.equal(ctx.pollBusy,false);
    check('stalled status request expires and releases pollBusy');
    await ctx.poll();assert.equal(statusCalls,2);assert.equal(ctx.pollBusy,false);assert.equal(ctx.dur,0);
    check('next status poll recovers without reloading page');
    for(const route of ['/queue','/queue/play']){
      const started=Date.now();await assert.rejects(ctx.timedFetch(route,route.endsWith('play')?{method:'POST'}:{}),e=>e.name==='AbortError');
      assert(Date.now()-started<1500);check(route+': stalled response body is aborted');
    }
    const empty=await ctx.timedFetch('/cmd/toggle',{method:'POST'});
    assert.equal(empty.status,204);assert.equal(await empty.text(),'');check('204 commands remain successful');
    const failure=await ctx.timedFetch('/error');assert.equal(failure.status,503);assert.equal((await failure.json()).err,'failure');
    check('HTTP error status and JSON remain available to caller');
    const invalid=await ctx.timedFetch('/invalid');await assert.rejects(invalid.json());check('invalid JSON remains an explicit failure');
  }finally{server.closeAllConnections();await new Promise(r=>server.close(r))}
  fs.writeFileSync(path.join(__dirname,'artifacts/audit-frontend-fixed-results.json'),JSON.stringify(results,null,2));
})().catch(e=>{server.closeAllConnections();server.close();console.error(e);process.exitCode=1});
