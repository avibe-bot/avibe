import type { Frame, FrameSpec } from './frame';

// Frame files load on demand: a document that draws one frame imports only the file it lives in,
// so a frame's load time does not grow with the number of frames.
const loaders = import.meta.glob<{ default: FrameSpec[] }>('./frames/**/*.design.tsx');

async function loadFile(path: string): Promise<Frame[]> {
  const file = path.replace('./frames/', '').replace(/\.design\.tsx$/, '');
  const module = await loaders[path]();
  return module.default.map((spec) => ({ ...spec, file, id: `${file}/${spec.name}` }));
}

/** Every frame, for the canvas and the frame list. */
export async function loadAllFrames(): Promise<Frame[]> {
  const frames = await Promise.all(Object.keys(loaders).map(loadFile));
  return frames.flat().sort((a, b) => a.id.localeCompare(b.id));
}

/** One frame by id (`<file>/<name>`), loading only its file. */
export async function loadFrame(id: string): Promise<Frame | undefined> {
  const path = `./frames/${id.slice(0, id.lastIndexOf('/'))}.design.tsx`;
  if (!(path in loaders)) return undefined;
  return (await loadFile(path)).find((frame) => frame.id === id);
}
