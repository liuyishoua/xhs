import { useRef } from 'react';
import { useKeepAliveContext } from 'keepalive-for-react';

// Read the latest visibility inside long-lived polling callbacks without resetting forms.
export function usePageVisible() {
  const { active } = useKeepAliveContext();
  const visible = useRef(active);
  visible.current = active;
  return () => visible.current && !document.hidden;
}
