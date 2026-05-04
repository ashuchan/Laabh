import { useEffect } from 'react';

/**
 * Binds [ and ] to step a date backward/forward.
 * Call from any page that supports day navigation.
 */
export function useDateStepShortcut(
  onPrev: () => void,
  onNext: () => void,
) {
  useEffect(() => {
    function handleKey(e: KeyboardEvent) {
      const tag = (e.target as HTMLElement).tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
      if (e.key === '[') onPrev();
      else if (e.key === ']') onNext();
    }
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [onPrev, onNext]);
}
