import { Icon } from '@/components/shared/Icon';
import type { Toast } from '@/types';

interface ToasterProps {
  toasts: Toast[];
}

export function Toaster({ toasts }: ToasterProps) {
  if (toasts.length === 0) return null;

  return (
    <div className="toasts">
      {toasts.map(t => (
        <div key={t.id} className={`toast toast-${t.kind}`}>
          <span className="toast-icon">
            <Icon name={t.kind === 'success' ? 'check' : t.kind === 'amber' ? 'bell' : 'dot'} size={12} />
          </span>
          <span>{t.text}</span>
        </div>
      ))}
    </div>
  );
}
