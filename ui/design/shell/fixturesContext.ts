import { createContext, useContext } from 'react';

import type { WorkbenchFixtures } from './fixtures';

export const FixturesContext = createContext<WorkbenchFixtures>({});

export const useDesignFixtures = (): WorkbenchFixtures => useContext(FixturesContext);
