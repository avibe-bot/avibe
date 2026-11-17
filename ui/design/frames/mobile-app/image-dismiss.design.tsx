// Approved 2026-11-17: the real preview opened through its chat-image consumer.
// Drag states are exercised in the component tests and browser touch capture;
// these static frames preserve the initial composition and reduced-motion mode.
import { ChatImage } from '@/components/ui/chat-image';
import { ImageViewerProvider } from '@/components/ui/image-viewer';
import photo from '../../assets/blog-header-night.jpg';
import chart from '../../assets/weekly-messages-chart.svg';
import { frames } from '../../frame';
import { IPHONE_17_PRO } from '../../shell/fixtures';

const designFrames = frames(['default', 'reduced-motion'].map((name) => ({
  name,
  title: `Image preview · ${name}`,
  ...IPHONE_17_PRO,
  status: 'shipped' as const,
  themes: ['light', 'dark'],
  fixtures: { bots: [] },
  reducedMotion: name === 'reduced-motion',
  click: ['img.cursor-zoom-in'],
  render: () => (
    <div className="h-full bg-bg p-6">
      <ImageViewerProvider images={[photo, chart]}>
        <ChatImage src={photo} />
      </ImageViewerProvider>
    </div>
  ),
})));

export default designFrames;
