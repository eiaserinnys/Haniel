import { Status } from '@/components/shared/Status';

export function Skeleton({ rows }: { rows: number }) {
  return (
    <div className="ledger" aria-busy="true">
      {Array.from({ length: rows }, (_, i) => (
        <div className="sk-row" aria-hidden="true" key={i}>
          <div className="sk-lines">
            <span className={`sk ${i % 2 ? 'w1' : 'w3'}`} />
            <span className={`sk meta ${i % 2 ? 'w3' : 'w2'}`} />
          </div>
          <span className="sk metric" />
        </div>
      ))}
    </div>
  );
}

interface LoadErrorProps {
  what: string;
  detail: string;
  onRetry: () => void;
}

export function LoadError({ what, detail, onRetry }: LoadErrorProps) {
  return (
    <div className="ledger">
      <div className="empty" role="alert">
        <Status state="error" />
        <p className="empty-title">Couldn&apos;t load {what}</p>
        <p className="empty-text mono sel">{detail}</p>
        <button type="button" className="btn btn-outline" onClick={onRetry}>Retry</button>
      </div>
    </div>
  );
}

interface EmptyProps {
  title?: string;
  text: string;
  inline?: boolean;
}

export function Empty({ title, text, inline = false }: EmptyProps) {
  return (
    <div className={inline ? 'empty is-inline' : 'empty'}>
      {title && <p className="empty-title">{title}</p>}
      <p className="empty-text">{text}</p>
    </div>
  );
}
