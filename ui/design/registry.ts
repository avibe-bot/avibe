import type { Frame, FrameSpec } from './frame';

const modules = import.meta.glob<{ default: FrameSpec[] }>('./frames/**/*.design.tsx', { eager: true });

export const ALL_FRAMES: Frame[] = Object.entries(modules)
  .flatMap(([path, module]) => {
    const file = path.replace('./frames/', '').replace(/\.design\.tsx$/, '');
    return module.default.map((spec) => ({ ...spec, file, id: `${file}/${spec.name}` }));
  })
  .sort((a, b) => a.id.localeCompare(b.id));
