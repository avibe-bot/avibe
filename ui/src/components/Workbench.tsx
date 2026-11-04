import { Navigate } from 'react-router-dom';

import { useBots } from '../context/BotsContext';
import { useInstanceAuthorization } from '../context/InstanceAuthorizationContext';
import { MainBotHome } from './home/MainBotHome';
import { MemberHome } from './home/MemberHome';

/**
 * The home at `/` (ui/design: home/home/*): one centered group whose last block is the composer.
 * The Owner talks to the main Bot there; a member, who has no Bots, starts a Session in a project.
 */
export const Workbench: React.FC = () => {
  const { capabilities } = useInstanceAuthorization();
  const { enabled: hasBots } = useBots();
  if (!capabilities.can_chat) return <Navigate to="/projects" replace />;
  return hasBots ? <MainBotHome /> : <MemberHome />;
};
