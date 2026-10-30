/* Test the actual page's latency calibration, independently of real audio. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../apple-music-remote.py'),'utf8');
// The production file may use CRLF line endings.
const actual=source.match(/function statusPosition\(s,sent,received\)\{[\s\S]*?\}\r?\nasync function poll/)[0].replace(/\r?\nasync function poll$/,'');
const context=vm.createContext({});vm.runInContext(actual,context);
const base={pos:10,dur:180,playing:true,sample_age_ms:80,processing_ms:120};
assert.equal(context.statusPosition(base,100,260),10.1);
assert.equal(context.statusPosition({...base,playing:false},100,260),10);
assert.equal(context.statusPosition({...base,pos:179.95},100,260),180);
assert.equal(context.statusPosition({...base,sample_age_ms:0,processing_ms:0},100,300),10.1);
assert.equal(context.statusPosition({...base,sample_age_ms:80,processing_ms:300},100,260),10.08);
assert.equal(context.statusPosition({...base,processing_ms:0},100,10100),12);
console.log('6 production playback-latency calibration cases passed');
