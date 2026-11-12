import fs from 'node:fs';
import {fork} from 'node:child_process';

export const instanceCases = JSON.parse(fs.readFileSync(new URL('./instance-cases.json', import.meta.url)));
export const instanceHosts = instanceCases.flatMap(c => ['control','a','b'].map(role => ({
  ...c, role, host:`${c.id}-${role}.webkit-proxy.test`, origin:c.protocol, proxy:c.protocol,
})));

export function instanceFixture(log, upstreamConnections) {
  let active = null, serial = 0;
  let controlQueue = Promise.resolve();
  const children = new Set();
  async function spawn(c, owner, port, stalled = false) {
    const listener = `${c.id}-${owner}-${++serial}`;
    const child = fork(new URL('./instance-listener.mjs', import.meta.url), ['--child'], {silent:true});
    children.add(child);
    const pending = new Map();
    let commandID = 0;
    child.stdout.on('data', data => log('instance_child_stdout', {case:c.id,listener,text:String(data)}));
    child.stderr.on('data', data => log('instance_child_stderr', {case:c.id,listener,text:String(data)}));
    child.on('error', error => log('instance_child_error', {case:c.id,listener,error:String(error)}));
    child.on('exit', (code,signal) => {
      children.delete(child);
      for (const {reject,timer} of pending.values()) { clearTimeout(timer); reject(new Error('child exited')); }
      pending.clear();
      log('instance_child_exit', {case:c.id,listener,pid:child.pid,code,signal});
    });
    child.on('message', message => {
      if (message.reply) {
        const entry = pending.get(message.reply);
        if (entry) { clearTimeout(entry.timer); pending.delete(message.reply);
          if (message.ok) entry.resolve(); else entry.reject(new Error(message.error)); }
        return;
      }
      const {event,...fields} = message;
      if (event === 'instance_upstream') {
        upstreamConnections.set(`${fields.sourcePort}:${fields.upstreamPort}`,fields.connection);
      } else if (event === 'instance_upstream_close') {
        for (const [key,value] of upstreamConnections) if (value === fields.connection) upstreamConnections.delete(key);
      }
      log(event, fields);
      if (fields.ack) child.send({ack:fields.ack});
    });
    const rpc = command => new Promise((resolve,reject) => {
      const token = `${listener}-${++commandID}`;
      const timer = setTimeout(() => { pending.delete(token); reject(new Error(`child deadline: ${command.command}`)); }, 5000);
      pending.set(token,{resolve,reject,timer});
      child.send({...command,token});
    });
    const worker = {child,rpc,owner,listener};
    active.workers.push(worker);
    await rpc({command:'init',config:{...c,listener,owner,epoch:owner === 'standin' ? null : port === 18100 ? 1 : 2,
      port,stalled,hosts:[`${c.id}-a.webkit-proxy.test`,`${c.id}-b.webkit-proxy.test`]}});
    return worker;
  }
  async function teardown(reason) {
    if (!active) return;
    log('instance_teardown', {case:active.c.id,reason});
    const workers = active.workers;
    active = null;
    await Promise.all(workers.map(({child}) => new Promise(resolve => {
      if (child.exitCode !== null || child.signalCode !== null) { resolve(); return; }
      const timer = setTimeout(() => { child.kill('SIGKILL'); resolve(); }, 2000);
      child.once('exit', () => { clearTimeout(timer); resolve(); });
      if (child.connected) child.disconnect();
    })));
  }
  async function handle(path, body) {
      if (path === '/instance/setup') {
        if (active) throw new Error('prior cell not torn down');
        const c = instanceCases.find(x => x.id === body.case);
        if (!c) throw new Error('unknown instance case');
        active = {c,workers:[],changed:false};
        log('instance_setup', {case:c.id,mode:c.mode,protocol:c.protocol,parentPID:process.pid});
        active.p = await spawn(c,c.mode === 'occupied-existing' ? 'standin' : 'listener(1)',18100);
        return {ok:true};
      }
      if (!active || active.c.id !== body.case) throw new Error('wrong active cell');
      if (path === '/instance/transition') {
        const {c,p} = active;
        if (c.question !== 'q4' || active.changed) throw new Error('invalid transition');
        active.changed = true;
        // Stand-in fault injection is test setup, preceding recovery. No recovery
        // command is ever sent to a stand-in, nor are its sockets in owned registry.
        if (c.mode === 'occupied-existing') await p.rpc({command:'stall'});
        log('instance_recovery_start', {case:c.id,owned:active.workers.filter(w => w.owner === 'listener(1)').map(w => w.listener)});
        if (p.owner === 'listener(1)') await p.rpc({command:'stop-owned'});
        if (c.mode === 'occupied-new') active.standin = await spawn(c,'standin',18100,true);
        active.p2 = await spawn(c,'listener(2)',18101);
        log('instance_recovery_ready', {case:c.id});
        return {ok:true};
      }
      if (path === '/instance/finish') {
        for (const worker of active.workers) {
          if (worker.owner === 'standin') await worker.rpc({command:'snapshot'});
        }
        log('instance_observation_end', {case:active.c.id});
        await teardown('cell-finished');
        return {ok:true};
      }
      throw new Error('unknown instance control');
  }
  return {
    handle(path, body) {
      const work = controlQueue.then(() => handle(path,body));
      controlQueue = work.catch(() => {});
      return work;
    },
    shutdown() { for (const child of children) if (child.connected) child.disconnect(); },
  };
}
