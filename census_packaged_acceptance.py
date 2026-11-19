"""Runner-only smoke: never collects on a live user install or publishes process identifiers."""
import json, os, pathlib, subprocess, tempfile, time
assert os.environ.get('RUNNER_OS') == 'macOS' and os.environ.get('GITHUB_ACTIONS') == 'true'
root=pathlib.Path(tempfile.mkdtemp(dir='/tmp',prefix='census-ci-')); home=root/'home'; home.mkdir(); app=root/'Avibe.app'
subprocess.run(['ditto','desktop/target/release/bundle/macos/Avibe.app',app],check=True)
sock=root/'dispatch.sock'; env={k:v for k,v in os.environ.items() if k in ('PATH','TMPDIR','LANG','USER','LOGNAME')}; env.update(HOME=str(home),AVIBE_HOME=str(home/'.avibe'),VIBE_INTERNAL_DISPATCH_SOCKET=str(sock)); start=time.monotonic()
p=subprocess.Popen([app/'Contents/MacOS/avibe-desktop'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
try:
 def arm():
  return json.loads(subprocess.check_output(['curl','--max-time','4','-fsS','--unix-socket',str(sock),'-X','POST','http://localhost/internal/diagnostics/warm-gc-census'],stderr=subprocess.DEVNULL))
 while time.monotonic()-start<360:
  assert p.poll() is None, 'packaged app exited'
  if sock.exists():
   try: reply=arm(); break
   except subprocess.CalledProcessError: pass
  time.sleep(2)
 else: raise RuntimeError('no arm acknowledgment from isolated app runtime')
 assert reply['armed'] is True
 pid=reply['controller_pid']; assert type(pid) is int and pid>1
 parent=pid; depth=0
 while parent>1 and parent!=p.pid and depth<20:
  parent=int(subprocess.check_output(['ps','-o','ppid=','-p',str(parent)]));depth+=1
 assert depth and parent==p.pid,'controller not descended from app'
 assert arm()=={'armed':False,'controller_pid':pid}
 (pathlib.Path(os.environ['RUNNER_TEMP'])/'census-counts.json').write_text(json.dumps({'arms_accepted':1,'repeat_arms_rejected':1,'ancestry_hops':depth,'elapsed_ms':round((time.monotonic()-start)*1000)}))
finally:
 if p.poll() is None:p.terminate();p.wait(timeout=30)
