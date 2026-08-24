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

const stringList = (value: unknown): string[] =>
  Array.isArray(value)
    ? value.filter((entry): entry is string => typeof entry === 'string')
    : [];

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
    // ``cli_path`` has a different owner from the wizard's enable toggle:
    // install_agent and the provider runtime card persist it at the moment
    // they discover or edit the path. Replaying that asynchronously derived
    // value on Continue would turn an already-committed install result back
    // into a stale browser snapshot. Keep the field in the step data for
    // display, but never emit it from this mutation builder.
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
  // A platform that was already enabled and runnable when the wizard loaded is
  // not owned by a later credential edit. Only newly selected platforms, or a
  // selected platform whose credentials were not runnable before this step,
  // may acquire a wizard-owned add operation. This keeps a stale credential
  // snapshot from turning Finish into an implicit re-enable.
  const needsWizardEnablement = (platform: string) =>
    !selectedBefore.includes(platform) || !platformHasRunnableConfig(previous, platform);
  const addCandidates = stepId === 'platform'
    ? [...new Set([
        ...newlySelected,
        ...editedPlatforms.filter(needsWizardEnablement),
      ])]
    : editedPlatforms.filter(needsWizardEnablement);
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

export type WizardEnabledPlatformDelta = {
  add: string[];
  remove: string[];
};

/**
 * Collect the list intent from mutations that were actually submitted by a
 * wizard step. The ordering mirrors configMutationsToPayload: a later add or
 * remove wins for the same platform.
 */
export const collectWizardEnabledPlatformDelta = (
  mutations: readonly ConfigMutation[],
): WizardEnabledPlatformDelta => {
  const add = new Set<string>();
  const remove = new Set<string>();
  for (const mutation of mutations) {
    if (mutation.kind !== 'enabled-platforms') continue;
    for (const platform of mutation.remove) {
      if (!platform) continue;
      add.delete(platform);
      remove.add(platform);
    }
    for (const platform of mutation.add) {
      if (!platform) continue;
      remove.delete(platform);
      add.add(platform);
    }
  }
  return { add: [...add], remove: [...remove] };
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
  // The loaded enabled list is an observation, not ownership. Finish may
  // reassert only list changes that this wizard actually submitted.
  const wizardAdds = stringList(config.__wizardEnabledAdds);
  const wizardRemoves = stringList(config.__wizardEnabledRemoves);
  const add = wizardAdds.filter(
    (platform) => selected.includes(platform) && platformHasRunnableConfig(config, platform),
  );
  const remove = [
    ...new Set([
      ...baseline.filter((platform) => !selected.includes(platform)),
      ...wizardRemoves.filter((platform) => !selected.includes(platform)),
    ]),
  ];
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
