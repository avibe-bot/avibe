import {
  configChanges,
  setConfigField,
  updateEnabledPlatforms,
  type ConfigMutation,
} from './configMutations';
import { getEnabledPlatforms, platformHasRunnableConfig } from './platforms';
import {
  withoutConfiguredSecretMarker,
  withSecretDraft,
  withSecretDrafts,
} from './secretFields';

const WIZARD_PLATFORMS = ['slack', 'discord', 'telegram', 'lark', 'wechat'] as const;
const WIZARD_BACKENDS = ['opencode', 'claude', 'codex'] as const;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const recordOrEmpty = (value: unknown): Record<string, unknown> =>
  isRecord(value) ? value : {};

const normalizePlatformSection = (platform: string, section: unknown) => {
  if (!isRecord(section)) return section;

  let normalized = { ...section };
  if (platform === 'slack') {
    normalized = withSecretDrafts(normalized, {
      bot_token: typeof normalized.bot_token === 'string' ? normalized.bot_token : undefined,
      app_token: typeof normalized.app_token === 'string' ? normalized.app_token : undefined,
    });
  } else if (platform === 'lark') {
    const appIdChanged = Boolean(
      normalized.original_app_id &&
      normalized.app_id &&
      normalized.app_id !== normalized.original_app_id,
    );
    normalized = withSecretDraft(
      appIdChanged
        ? withoutConfiguredSecretMarker(normalized, 'app_secret')
        : normalized,
      'app_secret',
      typeof normalized.app_secret === 'string' ? normalized.app_secret : undefined,
    );
  } else {
    normalized = withSecretDraft(
      normalized,
      'bot_token',
      typeof normalized.bot_token === 'string' ? normalized.bot_token : undefined,
    );
  }

  for (const key of Object.keys(normalized)) {
    if (key.startsWith('has_') || key.endsWith('_length')) delete normalized[key];
  }
  delete normalized.original_app_id;
  if (platform === 'discord') delete normalized.client_id;
  return normalized;
};

const agentMutations = (before: unknown, stepData: unknown): ConfigMutation[] => {
  const submittedAgents = recordOrEmpty(recordOrEmpty(stepData).agents);
  const previousAgents = recordOrEmpty(recordOrEmpty(before).agents);

  return WIZARD_BACKENDS.flatMap((backend) => {
    const submitted = submittedAgents[backend];
    if (!isRecord(submitted)) return [];

    const owned: Record<string, unknown> = {};
    if (typeof submitted.enabled === 'boolean') owned.enabled = submitted.enabled;
    if (typeof submitted.cli_path === 'string') owned.cli_path = submitted.cli_path;
    return configChanges(previousAgents[backend], owned, ['agents', backend]);
  });
};

const platformSectionMutations = (
  before: unknown,
  stepData: unknown,
  platforms: readonly string[],
): ConfigMutation[] => {
  const previous = recordOrEmpty(before);
  const submitted = recordOrEmpty(stepData);
  return platforms.flatMap((platform) => {
    if (submitted[platform] === undefined) return [];
    return configChanges(
      previous[platform],
      normalizePlatformSection(platform, submitted[platform]),
      [platform],
    );
  });
};

export const buildWizardStepMutations = ({
  stepId,
  before,
  stepData,
  after,
}: {
  stepId: string;
  before: unknown;
  stepData: unknown;
  after: unknown;
}): ConfigMutation[] => {
  if (stepId === 'agents') return agentMutations(before, stepData);

  const previous = recordOrEmpty(before);
  const submitted = recordOrEmpty(stepData);
  const next = recordOrEmpty(after);
  const editedPlatforms = stepId === 'platform'
    ? WIZARD_PLATFORMS.filter((platform) => submitted[platform] !== undefined)
    : stepId.startsWith('platform-')
      ? [stepId.slice('platform-'.length)]
      : [];
  if (editedPlatforms.length === 0 && stepId !== 'platform') return [];

  const mutations = platformSectionMutations(before, stepData, editedPlatforms);
  const selectedBefore = getEnabledPlatforms(previous);
  const selectedAfter = getEnabledPlatforms(next);
  const newlySelected = selectedAfter.filter((platform) => !selectedBefore.includes(platform));
  const addCandidates = stepId === 'platform'
    ? [...new Set([...newlySelected, ...editedPlatforms])]
    : editedPlatforms;
  const add = addCandidates.filter(
    (platform) =>
      selectedAfter.includes(platform) && platformHasRunnableConfig(next, platform),
  );
  const remove = stepId === 'platform'
    ? selectedBefore.filter((platform) => !selectedAfter.includes(platform))
    : [];
  if (add.length > 0 || remove.length > 0) {
    mutations.push(updateEnabledPlatforms({ add, remove }));
  }

  if (stepId === 'platform' && next.show_duration !== previous.show_duration) {
    mutations.push(setConfigField(['show_duration'], next.show_duration));
  }
  return mutations;
};

export const buildWizardFinishMutations = (
  data: unknown,
  autoUpdate: boolean,
): ConfigMutation[] => {
  const config = recordOrEmpty(data);
  const update = recordOrEmpty(config.update);
  const selected = getEnabledPlatforms(config);
  const baseline: string[] = Array.isArray(config.__wizardEnabledBaseline)
    ? config.__wizardEnabledBaseline.filter(
        (platform): platform is string => typeof platform === 'string',
      )
    : [];
  const add = selected.filter((platform) => platformHasRunnableConfig(config, platform));
  const remove = baseline.filter((platform) => !selected.includes(platform));
  const mutations: ConfigMutation[] = [];

  if (add.length > 0 || remove.length > 0) {
    mutations.push(updateEnabledPlatforms({ add, remove }));
  }
  if (update.auto_update !== autoUpdate) {
    mutations.push(setConfigField(['update', 'auto_update'], autoUpdate));
  }
  if (selected.includes('wechat') && config.show_duration !== false) {
    mutations.push(setConfigField(['show_duration'], false));
  }
  mutations.push(setConfigField(['setup_completed'], true));
  return mutations;
};
