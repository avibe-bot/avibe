// Direction B · 层次 (layers), proposed 2026-11-03: surfaces separated by tone rather than lines,
// borderless cards, the agent speaking as plain text, larger type and more air.
// The tokens and rules are in kit/directions/directions.css.
import { frames } from '../../frame';
import { directionFrames } from './surfaces';

const designFrames = frames(directionFrames('b', 'B 层次'));

export default designFrames;
