import { frames } from '../../../frame';
import { PlacementComparison } from '../../../kit/desktop-instance-placement';
import { placementFrames } from '../placement-fixtures';
const designFrames = frames(placementFrames('bottom').map(({ fixture, ...frame }) => ({
  ...frame, render: () => <PlacementComparison fixture={fixture} />,
})));

export default designFrames;
