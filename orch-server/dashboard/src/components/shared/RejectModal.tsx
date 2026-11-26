import { useRef, useState } from 'react';
import { Dialog } from '@base-ui/react/dialog';
import type { Deploy } from '@/types';

const PRESETS = [
  'WIP — 아직 검토 중',
  '의존성 충돌 의심',
  '관련 PR 머지 후 재시도',
  '오프시간 외 배포 금지',
] as const;

interface RejectModalProps {
  deploy: Deploy;
  onConfirm: (reason: string) => void;
  onClose: () => void;
}

export function RejectModal({ deploy, onConfirm, onClose }: RejectModalProps) {
  const [reason, setReason] = useState('');
  const reasonRef = useRef<HTMLTextAreaElement>(null);
  const isPostpone = Boolean(deploy.is_self_update);
  const action = isPostpone ? 'Postpone' : 'Reject deploy';

  const handleSubmit = () => {
    if (reason.trim()) onConfirm(reason.trim());
  };

  return (
    <Dialog.Root open onOpenChange={(open) => { if (!open) onClose(); }}>
      <Dialog.Portal>
        <Dialog.Backdrop className="scrim" />
        <Dialog.Popup className="dialog" initialFocus={reasonRef}>
          <div className="dialog-head">
            <Dialog.Title className="dialog-title">
              {isPostpone ? 'Postpone self-update' : 'Reject deploy'}
            </Dialog.Title>
            <Dialog.Description className="dialog-sub" render={<p />}>
              <span>{deploy.repo}</span>
              <span>{deploy.node_id}</span>
              <span>
                <span className="mono">{deploy.commits.length}</span> commit{deploy.commits.length !== 1 ? 's' : ''}
              </span>
            </Dialog.Description>
          </div>
          <div className="dialog-body">
            <label className="field">
              <span className="field-label">Reason</span>
              <textarea
                ref={reasonRef}
                className="textarea"
                rows={3}
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
            </label>
            <div className="presets">
              {PRESETS.map(preset => (
                <button key={preset} type="button" className="preset" onClick={() => setReason(preset)}>
                  {preset}
                </button>
              ))}
            </div>
          </div>
          <div className="dialog-foot">
            <button type="button" className="btn btn-outline" onClick={onClose}>Cancel</button>
            <button type="button" className="btn btn-danger" disabled={!reason.trim()} onClick={handleSubmit}>
              {action}
            </button>
          </div>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
