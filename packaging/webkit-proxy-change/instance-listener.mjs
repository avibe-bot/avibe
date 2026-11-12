import http from 'node:http';
import net from 'node:net';
import {fileURLToPath} from 'node:url';

export function destination(target, hosts, protocol) {
  if (!target?.endsWith(':443') || !hosts.includes(target.slice(0, -4))) return null;
  return {host:target.slice(0, -4), upstreamHost:'127.0.0.1', upstreamPort:protocol === 'h2' ? 18444 : 18443};
}
export function requireRecoveryOwner(owner) {
  if (owner !== 'listener(1)') throw new Error('recovery cannot stop foreign/P2 sockets');
}

export function pauseRelay(entry) {
  entry.client.unpipe(entry.upstream); entry.upstream.unpipe(entry.client);
  entry.client.pause(); entry.upstream.pause();
}

// Started only by instance-fixture with an IPC channel. Never invokes external tools.
async function childMain() {
  let config, server, stalled = false, serial = 0;
  const sockets = new Map(), acks = new Map();
  const send = value => process.send?.(value);
  const event = (name, fields = {}) => send({event:'instance_' + name,
    case:config.id, owner:config.owner, epoch:config.epoch, port:config.port,
    listener:config.listener, pid:process.pid, ...fields});
  const closed = socket => new Promise(resolve => {
    if (socket.closed) resolve(); else socket.once('close', resolve);
  });
  async function stopOwned() {
    requireRecoveryOwner(config.owner);
    const listenerClosed = new Promise(resolve => server.close(resolve));
    const entries = [...sockets.entries()];
    const waits = entries.flatMap(([,x]) => [closed(x.client), ...(x.upstream ? [closed(x.upstream)] : [])]);
    for (const [,x] of entries) { x.client.destroy(); x.upstream?.destroy(); }
    await Promise.all(waits); await listenerClosed;
    event('owned_closed', {connections:entries.map(([id]) => id)});
    event('listener', {state:'stopped'});
  }
  process.on('message', async message => {
    if (message.ack) { acks.get(message.ack)?.(); acks.delete(message.ack); return; }
    const {command, token} = message;
    try {
      if (command === 'init') {
        if (config) throw new Error('already initialized');
        config = message.config;
        if (![18100,18101].includes(config.port) || !['h1','h2'].includes(config.protocol)
            || !['listener(1)','listener(2)','standin'].includes(config.owner)
            || config.hosts.length !== 2
            || config.hosts.some(h => ![`${config.id}-a.webkit-proxy.test`,`${config.id}-b.webkit-proxy.test`].includes(h))) {
          throw new Error('invalid fixed fixture configuration');
        }
        stalled = config.stalled;
        server = http.createServer((req,res) => { res.writeHead(405); res.end(); });
        server.on('connection', client => {
          const id = `${config.listener}-socket-${++serial}`;
          client.fixtureID = id;
          sockets.set(id, {client});
          event('accept', {connection:id});
          client.on('error', error => event('socket_error', {connection:id, error:String(error)}));
          client.on('close', () => {
            sockets.get(id)?.upstream?.destroy(); sockets.delete(id);
            event('socket_close', {connection:id});
          });
        });
        server.on('connect', (req, client, head) => {
          const id = client.fixtureID;
          const target = destination(req.url, config.hosts, config.protocol);
          if (!target) { event('rejected', {connection:id}); client.destroy(); return; }
          event('connect', {connection:id, target:req.url, host:target.host, stalled});
          if (stalled) { client.pause(); return; }
          const upstream = net.connect({host:target.upstreamHost, port:target.upstreamPort});
          sockets.get(id).upstream = upstream;
          upstream.on('error', error => { event('upstream_error', {connection:id,error:String(error)}); client.destroy(); });
          upstream.on('close', () => { event('upstream_close', {connection:id}); client.destroy(); });
          upstream.once('connect', () => {
            // Parent registers upstream-port -> CONNECT before TLS can reach the backend.
            const ack = `${id}-mapped`;
            const timer = setTimeout(() => { acks.delete(ack); client.destroy(); upstream.destroy(); }, 3000);
            acks.set(ack, () => {
              clearTimeout(timer);
              if (client.destroyed || upstream.destroyed) return;
              if (stalled) { pauseRelay({client,upstream}); return; }
              client.write('HTTP/1.1 200 Connection Established\r\n\r\n');
              if (head.length) upstream.write(head);
              client.pipe(upstream); upstream.pipe(client);
            });
            event('upstream', {connection:id, host:target.host, sourcePort:upstream.localPort,
              upstreamHost:target.upstreamHost, upstreamPort:target.upstreamPort, ack});
          });
        });
        await new Promise((resolve,reject) => {
          server.once('error', reject);
          server.listen({port:config.port,host:'127.0.0.1',exclusive:true}, resolve);
        });
        event('listener', {state:'ready', stalled});
      } else if (command === 'stop-owned') {
        await stopOwned();
      } else if (command === 'stall') {
        if (config.owner !== 'standin') throw new Error('stall is a stand-in setup control only');
        stalled = true;
        for (const x of sockets.values()) {
          if (x.upstream) pauseRelay(x); else x.client.pause();
        }
        event('stall', {connections:[...sockets.keys()]});
      } else if (command === 'snapshot') {
        event('snapshot', {connections:[...sockets.keys()],stalled});
      } else throw new Error('unknown child control');
      send({reply:token,ok:true});
    } catch (error) { send({reply:token,ok:false,error:String(error)}); }
  });
  // Test teardown only: parent disconnects after the case observation boundary.
  process.on('disconnect', () => {
    for (const x of sockets.values()) { x.client.destroy(); x.upstream?.destroy(); }
    server?.close(); process.exit(0);
  });
}
if (process.argv[1] === fileURLToPath(import.meta.url) && process.argv[2] === '--child' && process.send) {
  await childMain();
}
