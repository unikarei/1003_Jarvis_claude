const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const V = require('../../voice/web/static/vad.js');
const P = require('../../voice/web/static/page.js');
function harness() {
  const elements = new Map(), requests = [], sources = [];
  const document = { getElementById(id) {
    if (!elements.has(id)) elements.set(id, { disabled: false, addEventListener() {},
      classList: { contains: () => false }, focus() {} });
    return elements.get(id);
  }};
  const audio = { currentTime: 1, sampleRate: 16000, destination: {}, createBufferSource() {
    const s = { connect() {}, disconnect() {}, stop() { this.stopped = true; },
      start(_, offset = 0) { this.offset = offset; } }; sources.push(s); return s;
  }};
  const context = { window: { JarvisVad: V, JarvisPage: P }, document, performance: { now: () => 100 },
    AbortController, Float32Array, Map, console, setTimeout, clearTimeout, setInterval, clearInterval,
    fetch: async (url, opts) => { requests.push({ url, opts }); return { ok: true, status: 200, json: async () => ({}) }; } };
  let code = fs.readFileSync(path.join(__dirname, '../../voice/web/static/app.js'), 'utf8');
  code = code.replace(/\}\)\(\);\s*$/, `window.test = {
    setup(a) { audio=a; session='sid'; turn=1; awaiting=true; turnDone=false;
      config={min_threshold:.002,threshold_factor:4,max_seconds:20};
      detector=V.createDetector({threshold:.01}); },
    playItem, beginCandidate, finishCandidate, probeSpeech, resetPlayback, onTurnDone, onFrame,
    showApproval, answerApproval, clearApproval,
    get candidate() { return candidate; }, get source() {return source;},
    get awaiting() {return awaiting;}, get cut() {return cut;},
    record() { current = [new Float32Array(160)]; },
    get current() {return current;},
  }; })();`);
  vm.runInNewContext(code, context);
  const api = context.window.test; api.setup(audio);
  return { api, audio, requests, sources, elements, context };
}
test('noise resumes the same sentence at its offset without cancelling or duplicating start', async () => {
  const h = harness(), item = { id: 'audio', buffer: { duration: 5 } };
  h.api.playItem(item, 1); h.audio.currentTime = 1.7;
  h.api.beginCandidate(1.7, 11);
  assert.equal(h.sources[0].stopped, true);
  assert.equal(h.api.source, null);
  h.api.record(); h.api.finishCandidate(h.api.candidate, false);
  assert.ok(Math.abs(h.sources[1].offset - .7) < .001);
  assert.equal(h.api.current.noise, true);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.requests.filter(r => r.url.includes('/interrupt')).length, 0);
  assert.equal(h.requests.filter(r => r.url.includes('/utterance')).length, 0);
  assert.equal(h.requests.filter(r => r.url.includes('/played')).length, 1);
});
test('confirmed speech interrupts once; repeated or stale results do nothing', async () => {
  const h = harness(); h.api.beginCandidate(1, 11); const c = h.api.candidate;
  h.api.finishCandidate(c, true); h.api.finishCandidate(c, true);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.requests.filter(r => r.url.includes('/interrupt')).length, 1);
  assert.equal(h.api.awaiting, false);
  h.api.resetPlayback(2); h.api.finishCandidate(c, true);
  assert.equal(h.api.cut, 1);
});
test('short impulse produces no irreversible interruption or utterance', async () => {
  const h = harness();
  const frame = new Float32Array(160);
  for (let i = 0; i < 15; i++) h.api.onFrame(.1, frame, i / 100);
  for (let i = 0; i < 100; i++) h.api.onFrame(0, frame, .15 + i / 100);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.api.candidate, null);
  assert.equal(h.requests.filter(r => /\/interrupt|\/utterance/.test(r.url)).length, 0);
});
test('old probe cannot resume audio after new turn or turn_done', () => {
  for (const action of ['resetPlayback', 'onTurnDone']) {
    const h = harness(); h.api.playItem({id:'a',buffer:{duration:5}},1);
    h.api.beginCandidate(1,11); const c=h.api.candidate;
    h.api[action](action === 'resetPlayback' ? 2 : 1);
    h.api.finishCandidate(c,false);
    assert.equal(h.sources.length,1); assert.equal(h.api.source,null);
  }
});

test('failed and timed-out confirmation resume without cancelling', async () => {
  for (const mode of ['error', 'timeout']) {
    const h = harness(); h.api.playItem({id:'a',buffer:{duration:5}},1);
    await new Promise(resolve => setImmediate(resolve));
    h.api.beginCandidate(1,11); const c=h.api.candidate;
    h.context.fetch = () => mode === 'error' ? Promise.reject(new Error('offline')) : new Promise(() => {});
    h.api.probeSpeech(c,[new Float32Array(8000)]);
    await new Promise(resolve => setTimeout(resolve, mode === 'timeout' ? 1550 : 10));
    assert.equal(h.api.candidate,null); assert.equal(h.sources.length,2);
    assert.equal(h.api.cut,-1);
  }
});
test('voice confirmed during recording interrupts and uploads exactly once', async () => {
  const h=harness(); const frame=new Float32Array(160);
  h.context.fetch=async (url,opts) => {
    h.requests.push({url,opts});
    return {ok:true,status:200,json:async()=> url.includes('speech-check') ? {speech:true} : {turn:2}};
  };
  for(let i=0;i<50;i++) h.api.onFrame(.1,frame,i/100);
  await new Promise(resolve=>setImmediate(resolve));
  for(let i=0;i<30;i++) h.api.onFrame(.1,frame,.5+i/100);
  for(let i=0;i<100;i++) h.api.onFrame(0,frame,.8+i/100);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(h.requests.filter(r=>r.url.includes('/interrupt')).length,1);
  assert.equal(h.requests.filter(r=>r.url.includes('/utterance')).length,1);
});

test('approval is held during confirmation and restored only before its deadline', async () => {
  for (const expires of [30, 0]) {
    const h=harness();
    h.api.showApproval({turn:1,id:'approval',expires_in:expires,text:'確認'});
    h.api.beginCandidate(1,11);
    assert.equal(h.elements.get('approve-run').disabled,true);
    h.api.answerApproval('run');
    h.api.finishCandidate(h.api.candidate,false);
    assert.equal(h.elements.get('approve-run').disabled,expires === 0);
    assert.equal(h.requests.length,0);
    h.api.clearApproval();
  }
});
