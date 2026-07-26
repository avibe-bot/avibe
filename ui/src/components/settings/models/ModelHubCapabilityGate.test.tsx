import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import {
  MODEL_HUB_DISABLED_REDIRECT,
  MODEL_HUB_SETTINGS_PATH,
  ModelHubRenderBoundary,
  modelHubRouteTarget,
} from './ModelHubCapabilityGate';

describe('Model Hub capability route gate', () => {
  it('redirects both current and legacy model routes while disabled', () => {
    expect(modelHubRouteTarget(MODEL_HUB_SETTINGS_PATH, false)).toBe(MODEL_HUB_DISABLED_REDIRECT);
    expect(modelHubRouteTarget('/settings/models', false)).toBe(MODEL_HUB_DISABLED_REDIRECT);
  });

  it('does not render the Models child while disabled or unresolved', () => {
    let childRenders = 0;
    const ModelsChunk = () => {
      childRenders += 1;
      return <div>models chunk</div>;
    };

    const disabledHtml = renderToStaticMarkup(
      <ModelHubRenderBoundary enabled={false} disabled={<div>disabled</div>}>
        <ModelsChunk />
      </ModelHubRenderBoundary>,
    );
    const unresolvedHtml = renderToStaticMarkup(
      <ModelHubRenderBoundary enabled={null}>
        <ModelsChunk />
      </ModelHubRenderBoundary>,
    );

    expect(disabledHtml).toContain('disabled');
    expect(unresolvedHtml).toBe('');
    expect(childRenders).toBe(0);
  });

  it('renders the Models child only after the backend enables it', () => {
    let childRenders = 0;
    const ModelsChunk = () => {
      childRenders += 1;
      return <div>models chunk</div>;
    };

    expect(
      renderToStaticMarkup(
        <ModelHubRenderBoundary enabled>
          <ModelsChunk />
        </ModelHubRenderBoundary>,
      ),
    ).toContain('models chunk');
    expect(childRenders).toBe(1);
  });
});
