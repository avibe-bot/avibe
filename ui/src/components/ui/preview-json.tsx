import JsonView from '@uiw/react-json-view';
import { githubDarkTheme } from '@uiw/react-json-view/githubDark';
import { githubLightTheme } from '@uiw/react-json-view/githubLight';

import { useTheme } from '@/context/ThemeContext';

// The interactive JSON tree, isolated in its own module so the shared preview kernel can
// ``React.lazy`` it — @uiw/react-json-view and its two themes then split into a chunk that loads only
// when a JSON file is actually previewed, never touching the apps/main bundle. The caller (JsonBlock
// in file-preview) has already decided the value is an object/array small enough to mount as a tree.
export default function PreviewJson({ value }: { value: object }) {
  const { resolvedTheme } = useTheme();
  return (
    <JsonView
      value={value}
      style={resolvedTheme === 'light' ? githubLightTheme : githubDarkTheme}
      collapsed={2}
      displayDataTypes={false}
      shortenTextAfterLength={0}
      className="vr-fileview-json"
    />
  );
}
