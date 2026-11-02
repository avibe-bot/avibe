// What the design-system frames read: the Owner's Bots, which the real Bots provider fetches on every
// frame (and a mention chip names), plus whatever a frame adds.
import { demoBots, type WorkbenchFixtures } from '../../shell/fixtures';

export const systemFixtures = (api: Record<string, unknown> = {}): WorkbenchFixtures => ({ bots: demoBots, api });
