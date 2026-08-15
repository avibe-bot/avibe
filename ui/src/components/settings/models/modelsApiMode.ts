import { loadMockModelsApiForMode } from './featureFlags';
import {
  installLiveModelsApi,
  installModelsApi,
  type ModelsApi,
} from './modelsApi';
import { SettingsModelsPage } from './SettingsModelsPage';

let configuredClient: Promise<ModelsApi> | null = null;

/** Select the client before importing any component that consumes its facade. */
export const configureModelsApi = (): Promise<ModelsApi> => {
  if (configuredClient) return configuredClient;
  configuredClient = loadMockModelsApiForMode
    ? loadMockModelsApiForMode().then((client) => {
        installModelsApi(client);
        return client;
      })
    : Promise.resolve(installLiveModelsApi());
  return configuredClient;
};

export const loadSettingsModelsPage = async () => {
  await configureModelsApi();
  return { default: SettingsModelsPage };
};
