// Pure tests: no sockets, child processes, browser or Apple tools.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {instanceScript} from './instance-script.mjs';
import {destination, pauseRelay, requireRecoveryOwner} from './instance-listener.mjs';

const swift = fs.readFileSync(new URL('./InstanceRouteTests.swift',import.meta.url),'utf8');
const bridgeMatch = /web\.callAsyncJavaScript\("([^"\n]+)" \+ expression \+ "([^"\n]+)"/.exec(swift);
assert.ok(bridgeMatch,'actual Swift bridge not found');
const bridge = expression => bridgeMatch[1] + expression + bridgeMatch[2];
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
const fixture = fs.readFileSync(new URL('./fixture.mjs',import.meta.url),'utf8');
const originalScript = /const script = String\.raw`([\s\S]*?)`;/m.exec(fixture)[1];
let timerID = 0;
const timers = new Map();
let mode = 'ok', requests = [];
const fakeFetch = async (url, options) => {
  requests.push({url,options});
  if (mode === 'reject') throw new TypeError('Load failed');
  if (mode === 'stall') return new Promise((resolve,reject) => {
    options.signal.addEventListener('abort',() => reject(new Error('aborted')),{once:true});
  });
  return {ok:true,json:async()=>({id:new URL(url).searchParams.get('id'),route:'proxy-backend',host:new URL(url).hostname})};
};
const window = {};
vm.runInNewContext(originalScript.replace('PAGE_PAYLOAD','{"id":"page","route":"origin"}') + instanceScript,
  {window,fetch:fakeFetch,AbortController,Map,encodeURIComponent,
    setTimeout:(fn,ms)=>{ const id=++timerID; timers.set(id,{fn,ms}); return id; },
    clearTimeout:id=>timers.delete(id)});
const run = (expression,id='x') => new AsyncFunction('window','id','host',bridge(expression))(window,id,'q2-h1-a.webkit-proxy.test').then(JSON.parse);
assert.equal((await run('window.probe.page')).id,'page');
assert.equal((await run('window.instanceProbe.fetch(id, host)')).id,'x');
assert.equal(requests[0].options.cache,'no-store');
assert.equal(window.instanceProbe.pending.size,0);
assert.equal(timers.size,0);
// The old no-await serialization is the negative control for this exact async function.
mode='ok';
assert.equal(await new AsyncFunction('window','id','host','return JSON.stringify(window.instanceProbe.fetch(id,host));')(window,'bad','q2-h1-a.webkit-proxy.test'),'{}');
await window.instanceProbe.pending.get('bad')?.promise;
await Promise.resolve();
mode='reject';
assert.equal((await run('window.instanceProbe.fetch(id,host)','fail')).outcome,'failed');
mode='stall';
const blocked = run('window.instanceProbe.fetch(id,host)','slow');
assert.equal(window.instanceProbe.pending.size,1);
const abortTimer = [...timers.values()].find(t=>t.ms===8000);
assert.ok(abortTimer); abortTimer.fn();
assert.equal((await blocked).outcome,'bounded_failure');
assert.equal(window.instanceProbe.pending.size,0);
mode='ok';
assert.equal((await run('window.instanceProbe.fetch(id,host)','next')).outcome,'routed');
mode='stall';
const cancelling = run('window.instanceProbe.fetch(id,host)','cancelled');
assert.equal((await run('window.instanceProbe.cancel(id)','cancelled')).settled,true);
assert.equal((await cancelling).settled,true);
assert.equal(window.instanceProbe.pending.size,0);
assert.equal(timers.size,0);
const hosts=['q2-h1-a.webkit-proxy.test','q2-h1-b.webkit-proxy.test'];
assert.deepEqual(destination(hosts[0]+':443',hosts,'h1'),{host:hosts[0],upstreamHost:'127.0.0.1',upstreamPort:18443});
for (const target of ['example.com:443',hosts[0]+':80','127.0.0.1:443',hosts[0]+'.evil:443']) assert.equal(destination(target,hosts,'h2'),null);
requireRecoveryOwner('listener(1)');
for (const owner of ['standin','listener(2)','unknown']) assert.throws(()=>requireRecoveryOwner(owner));
const calls=[];
const socket = name => ({unpipe:()=>calls.push(name+' unpipe'),pause:()=>calls.push(name+' pause'),destroy:()=>{throw Error('must not close stand-in');}});
pauseRelay({client:socket('client'),upstream:socket('upstream')});
assert.deepEqual(calls,['client unpipe','upstream unpipe','client pause','upstream pause']);
console.log('actual fixture + extracted Swift async bridge: success/failure/abort/drain/next-operation, allowlist and non-closing stall controls passed');
