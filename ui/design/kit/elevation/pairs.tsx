import type { FrameSpec } from '../../frame';
import { ElevationProposal } from './ElevationProposal';

/** The frame named `state` in a design file's frames. */
export const pick = (file: string, specs: FrameSpec[], state: string): FrameSpec => {
  const spec = specs.find((candidate) => candidate.name === state);
  if (!spec) throw new Error(`no frame ${file}/${state}`);
  return spec;
};

/** A frame drawn unchanged under the approved system; `of` names the frame it pairs with. */
export const approved = (spec: FrameSpec, name: string, of: string, title = spec.title): FrameSpec => ({
  ...spec,
  name,
  title: `定稿 · ${title.replace(/^现状 · /, '')}（对照 ${of}）`,
  status: 'approved',
  render: () => <ElevationProposal>{spec.render()}</ElevationProposal>,
});
