import { expect, test } from '@playwright/test';
import { ORIGIN, serveProduct } from './support';
import { readFileSync } from 'node:fs';
const en=JSON.parse(readFileSync(new URL('../../src/i18n/en.json', import.meta.url),'utf8'));

for (const [width,chosen] of [[1200,"claude"],[390,"claude"],[1200,"codex"]] as const) {
 test(`normal setup route Save entry reload ${width} ${chosen}`, async ({page},info) => {
  await page.setViewportSize({width,height:844});
  const denied=await serveProduct(page);
  let completed=false;
  let defaultName='claude';
  const hops=[{source_id:'src_fixture',model_id:'gpt-5'},{source_id:'src_fixture',model_id:'gpt-4.1'}];
  type Write={setup_completed?:boolean,name?:string,hops?:typeof hops,manual_override?:{hops:typeof hops}};
  const writes: Array<{path:string,body:Write|null}>=[];
  const errors:string[]=[]; page.on('pageerror', e=>errors.push(e.message));
  const saved:Record<string,typeof hops>={claude:[],codex:[]};
  const agent=(backend:string)=>({id:`fixture-${backend}`,name:backend,backend,model:backend==='claude'?'opus-5':'gpt-5',enabled:true,archived:false,metadata:{builtin_default:true},source:'builtin',display_name:backend,description:'',system_prompt:null,reasoning_effort:null});
  const config=()=>({version:'v2',mode:'self_host',setup_completed:completed,setup_state:{needs_setup:!completed},capabilities:{model_hub:{enabled:true}},platforms:{primary:'slack',enabled:[]},runtime:{},agents:Object.fromEntries(['claude','codex','opencode'].map(b=>[b,{enabled:b!=='opencode',cli_path:b}])),agent:{default_cwd:'/fixture/work'},model_hub:{enabled:true,runtime_default_applied:true}});
  const source={id:'src_fixture',kind:'api_key',vendor:'openai',display_name:'Fixture source',protocol:'openai_chat',supply_channel:'hub',billing:'metered',state:{status:'active'},masked_credential:'fixture-only',models:hops.map(h=>({id:h.model_id,origin:'discovered',reasoning_efforts:[],reasoning_efforts_source:null})),last_discovered_at:null};
  const supply=(b:string)=>({backend:b,cli_present:true,mode:'hub',menu_kind:'fixed',sources:{order:[source.id],eligibility:[{source_id:source.id,eligible:true}]},routes:{},builtin_models:[],catalog_models:[],menu:null,model_supply:[],named_agents:[{name:b,effective_model_id:agent(b).model,supply_status:saved[b].length?'ok':'unavailable'}],supply_status:saved[b].length?'ok':'unavailable'});
  const chain=(b:string,hs=saved[b])=>({contract_version:10,backend:b,model_id:agent(b).model,manual_override:hs.length?{hops:hs}:null,route_origin:hs.length?'manual':'automatic',current:hs[0]??null,chain:hs.map(h=>({...h,channel:'hub',health:'healthy',runnable:true,reason:null,retry_at:null})),supply_state:hs.length?'ok':'unavailable'});
  await page.addInitScript(()=>localStorage.setItem('i18nextLng','en'));
  await page.route(`${ORIGIN}/api/**`,async route=>{
   const req=route.request(),path=new URL(req.url()).pathname;
   const answer=(json:unknown)=>route.fulfill({json});
   const body:Write|null=req.method()==='GET'?null:req.postDataJSON() as Write;
   if(req.method()!=='GET') writes.push({path,body});
   if(path==='/api/config'){if(body?.setup_completed===true)completed=true;return answer(config());}
   if(path==='/api/session')return answer({remote:false,instance_kind:'personal'});
   if(path==='/api/version')return answer({current:'1.0.0',latest:'1.0.0',has_update:false,error:null});
   if(path==='/api/agents/default'){
    if(typeof body?.name==='string') defaultName=body.name;
    return answer({ok:true,default_agent_name:defaultName,agent:agent(defaultName)});
   }
   if(path==='/api/agents')return answer({ok:true,default_agent_name:defaultName,agents:[agent('claude'),agent('codex')]});
   if(/^\/api\/agents\/(claude|codex)$/.test(path))return answer({ok:true,agent:agent(path.split('/').at(-1)!),default_agent_name:defaultName});
   if(path==='/api/models/runtime/status')return answer({ok:true,runtime:{contract_version:10,enabled:true,host_platform:'linux',manifest:{name:'cliproxyapi',resolution:'resolved',version:'fixture',source_sha:'a'.repeat(40),assets:[]},status:{installed_version:'fixture',verified:true,health:'ok'}}});
   if(path==='/api/models/sources')return answer({ok:true,sources:[source]});
   if(path==='/api/models/agents')return answer({ok:true,agents:[supply('claude'),supply('codex')]});
   if(/\/api\/models\/agents\/(claude|codex)\/chain/.test(path)){
    const b=path.split('/')[4];
    if(req.method()==='PUT'){saved[b]=body?.hops??[];return answer({ok:true,chain:chain(b)});}
    if(path.endsWith('/preview'))return answer({ok:true,chain:chain(b,body?.manual_override?.hops??[])});
    return answer({ok:true,chain:chain(b)});
   }
   if(/\/api\/backend\/[^/]+\/connection$/.test(path)){
    const b=path.split('/')[3],ready=!!saved[b]?.length;
    return answer({ok:true,backend:b,enabled:b!=='opencode',installed:true,supply_mode:'hub',auth:'api_key',application:'applied',ready,entry_eligible:ready});
   }
   if(path==='/api/memory/status')return answer({ok:true,enabled:false});
   if(path==='/api/projects')return answer({ok:true,projects:[]});
   if(path==='/api/scopes')return answer({ok:true,scopes:[]});
   if(path==='/api/inbox')return answer({sessions:[],next_cursor:null,unread_by_session:{},unread_total:0,unread_sessions:0});
   if(path==='/api/events')return route.fulfill({status:204});
   return route.fallback();
  });
  await page.goto('/setup');
  await page.getByRole('button',{name:'Get started',exact:true}).click();
  const primary=page.locator('.onboarding-primary-action');
  await expect(page.locator('[data-setup-screen="providers"]')).toBeVisible();
  try { await expect(primary).toBeEnabled(); } catch(e) { console.log('DIAGNOSTIC',JSON.stringify({errors,denied,writes,dom:await page.locator('.onboarding-shell').evaluate(e=>e.outerHTML)})); throw e;} await primary.click();
  const card=page.locator(`[data-setup-screen-root="assistants"] .onboarding-assistant[aria-label="${chosen==='claude'?'Claude Code':'Codex'}"]`);
  await card.getByRole('button',{name:en.onboarding.setup.configureRoute,exact:true}).click();
  const add=()=>page.getByRole('button',{name:en.settings.models.routeDialog.addHop,exact:true});
  await add().click();await page.getByRole('option',{name:/gpt-5/}).click();await page.getByRole('button',{name:en.settings.models.routeDialog.add.confirm,exact:true}).click();
  await add().click();await page.getByRole('option',{name:/gpt-4.1/}).click();await page.getByRole('button',{name:en.settings.models.routeDialog.add.confirm,exact:true}).click();
  await page.screenshot({path:info.outputPath('route.png'),fullPage:true});
  await page.getByRole('button',{name:en.onboarding.route.done,exact:true}).click();
  await expect.poll(()=>saved[chosen]).toEqual(hops);
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await card.getByRole('button',{name:en.onboarding.setup.configureRoute,exact:true}).click();
  await expect(page.getByText('Fixture source · gpt-4.1',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:en.onboarding.route.done,exact:true}).click();
  await page.screenshot({path:info.outputPath('assistants.png'),fullPage:true});
  await page.getByRole('button',{name:'Enter workspace',exact:true}).click();
  await expect.poll(()=>completed).toBe(true);
  await expect(page).toHaveURL(`${ORIGIN}/`);
  await page.reload();await expect(page).toHaveURL(`${ORIGIN}/`);
  await expect(page.locator('.onboarding-shell')).toHaveCount(0);
  expect(errors).toEqual([]);
  expect(writes.filter(w=>w.path==='/api/config').at(-1)?.body?.setup_completed).toBe(true);
  expect(denied.filter(r=>/PUT|POST.*(?:auth|runtime)/.test(r))).toEqual([]);
 });
}
