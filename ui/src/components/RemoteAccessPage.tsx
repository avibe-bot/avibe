import React from 'react';

import { RemoteAccess } from '@/components/RemoteAccess';

// Remote Access was promoted from a block inside the Service settings page to its
// own sidebar destination (/admin/remote-access). The RemoteAccess component owns
// the full pairing/tunnel UI including its own header, so this page is a thin host.
export const RemoteAccessPage: React.FC = () => <RemoteAccess />;

export default RemoteAccessPage;
