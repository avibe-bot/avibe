import { GuardImpact } from '../../src/components/settings/models/GuardImpact';

/** The shared body deliberately has no dialog ancestor. */
export function TokenScopeFixture() {
  return <section data-testid="token-scope-body">
    <div className="model-hub-guard-body">
      <GuardImpact
        hops={[{ backend: 'codex', menu_model: '模型/gpt-test', source_id: 'src_test', model_id: 'gpt-test', position: 1 }]}
        gaps={[{ backend: 'codex', model_id: '模型/gpt-test', agents: ['Release bot', '发布助手'] }]}
        sourceNames={{ src_test: 'Provider A' }}
      />
    </div>
  </section>;
}
