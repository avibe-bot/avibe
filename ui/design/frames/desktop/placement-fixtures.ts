import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import type { FrameSpec } from '../../frame';
import type { PlacementFixture } from '../../kit/desktop-instance-placement';
import type { InstancePlacement, PlacementStatus } from '../../kit/desktop-instances';
import { workbenchFixtures, homeFactsApi } from '../workbench/fixtures';
import { agentsApi } from '../agents/fixtures';
import { chatFixtures } from '../chat/fixtures';
import { copyFor } from './instances-fixtures';

export function placementFrames(placement: InstancePlacement): (Omit<FrameSpec, 'render'> & { fixture: PlacementFixture })[] {
 const pages = ['home','chat','agents'] as const;
 const scenes: {name:string;page:PlacementFixture['page'];status:PlacementStatus;compact?:boolean;menu?:boolean}[] = pages.map(page=>({name:page,page,status:'signed-in'}));
 scenes.push(
   {name:'signed-out',page:'home',status:'signed-out'},
   {name:'update',page:'home',status:'update'},
   {name:'switching',page:'home',status:'switching'},
   {name:'collapsed',page:'home',status:'update',compact:true},
   {name:'menu',page:'home',status:'update',menu:true},
 );
 return scenes.flatMap(scene => (placement === 'bottom' ? ['zh','en'] as const : ['zh'] as const).map(lang=>({
   name:`${scene.name}-${lang}`,title:`${placement === 'top' ? 'A · Sidebar brand (rejected history)' : 'C · Two-row sidebar footer (selected)'} · ${scene.name} · ${lang}`,
   width:1200,height:800,themes:placement === 'bottom' ? ['light','dark'] : ['light'],status:'proposed',
   route:scene.page === 'agents' ? '/agents?tab=definitions&agent=reviewer' : '/',
   fixture:{state:'instances',lang,placement,page:scene.page,status:scene.status,compact:scene.compact,menu:scene.menu},
   fixtures:workbenchFixtures({...chatFixtures().api,...agentsApi('reviewer'),...homeFactsApi({model:true,push:true,computerUse:true,chatApps:true,paired:true}),'/api/session':{remote:false}}, {copy:copyFor(lang === 'en' ? en : zh)}),
 })));
}
