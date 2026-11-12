import { frames } from '../../frame';
import { DesktopInstances } from '../../kit/desktop-instances';
import { desktopInstanceFrames } from './instances-fixtures';

const designFrames = frames(desktopInstanceFrames.map(({ fixture, ...frame }) => ({
  ...frame,
  render: () => <DesktopInstances fixture={fixture} />,
})));

export default designFrames;
