import { Icon } from '@/components/shared/Icon';
import type { Toast } from '@/types';

interface ToasterProps {
  toasts: Toast[];
  onDismiss: (id: string) => void;
}

// Keep hyphenated words (repo, node, and service names) on one line.
function renderText(text: string) {
  return text.split(/(\S*-\S*)/).map((part, i) =>
    /-/.test(part) ? <span className="nw" key={i}>{part}</span> : part,
  );
}

export function Toaster({ toasts, onDismiss }: ToasterProps) {
  if (toasts.length === 0) return null;

  return (
    <div className="toasts" role="status" aria-live="polite">
      {toasts.map(t => {
        const fail = t.kind === 'error';
        return (
          <div key={t.id} className={fail ? 'toast is-fail' : 'toast'}>
            <span className={`st st-${fail ? 'fail' : 'ok'}`}><span>{renderText(t.text)}</span></span>
            <button type="button" className="icon-btn" aria-label="Dismiss" onClick={() => onDismiss(t.id)}>
              <Icon name="x" small />
            </button>
          </div>
        );
      })}
    </div>
  );
}
