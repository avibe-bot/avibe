import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import type { FrameSpec } from '../../frame';
import type { PlacementFixture } from '../../kit/desktop-instance-placement';
import type { InstancePlacement, ConnectionStatus } from '../../kit/desktop-instances';
import { workbenchFixtures, homeFactsApi } from '../workbench/fixtures';
import { agentsApi } from '../agents/fixtures';
import { chatFixtures } from '../chat/fixtures';
import { copyFor } from './instances-fixtures';

export function placementFrames(placement: InstancePlacement): (Omit<FrameSpec, 'render'> & { fixture: PlacementFixture })[] {
 const pages = ['home','chat','agents'] as const;
 const scenes: {name:string;page:PlacementFixture['page'];connection:ConnectionStatus;updateAvailable:boolean;updatePanel?:boolean;compact?:boolean;menu?:boolean}[] = pages.map(page=>({name:page,page,connection:'signed-in',updateAvailable:false}));
 scenes.push(
   {name:'signed-out',page:'home',connection:'signed-out',updateAvailable:true},
   {name:'update',page:'home',connection:'signed-in',updateAvailable:true,updatePanel:true},
   {name:'switching',page:'home',connection:'switching',updateAvailable:true},
   {name:'collapsed',page:'home',connection:'signed-in',updateAvailable:true,compact:true},
   {name:'menu',page:'home',connection:'signed-in',updateAvailable:false,menu:true},
 );
 return scenes.flatMap(scene => (placement === 'bottom' ? ['zh','en'] as const : ['zh'] as const).map(lang=>({
   name:`${scene.name}-${lang}`,title:`${placement === 'top' ? 'A · Sidebar brand (rejected history)' : 'C · Two-row sidebar footer (selected)'} · ${scene.name} · ${lang}`,
   width:1200,height:800,themes:placement === 'bottom' ? ['light','dark'] : ['light'],status:'proposed',
   route:scene.page === 'agents' ? '/agents?tab=definitions&agent=reviewer' : '/',
   fixture:{state:'instances',lang,placement,page:scene.page,connection:scene.connection,updateAvailable:scene.updateAvailable,updatePanel:scene.updatePanel,compact:scene.compact,menu:scene.menu},
   fixtures:workbenchFixtures({...chatFixtures().api,...agentsApi('reviewer'),...homeFactsApi({model:true,push:true,computerUse:true,chatApps:true,paired:true}),'/api/session':{remote:false}}, {copy:copyFor(lang === 'en' ? en : zh)}),
 })));
}
