import { useState } from 'react';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
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

  const handlePreset = (preset: string) => {
    setReason(preset);
  };

  const handleSubmit = () => {
    if (reason.trim()) {
      onConfirm(reason.trim());
    }
  };

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="sm:max-w-md" showCloseButton={false}>
        <DialogHeader>
          <DialogTitle>Reject deploy</DialogTitle>
          <DialogDescription>
            {deploy.repo} · {deploy.node_id} · {deploy.commits.length} commit{deploy.commits.length !== 1 ? 's' : ''}
          </DialogDescription>
        </DialogHeader>

        <div className="reject-body">
          <textarea
            className="reject-textarea"
            placeholder="Reason for rejection..."
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            rows={3}
            autoFocus
          />
          <div className="reject-presets">
            {PRESETS.map(preset => (
              <button
                key={preset}
                className="reject-preset-btn"
                onClick={() => handlePreset(preset)}
              >
                {preset}
              </button>
            ))}
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button
            variant="destructive"
            disabled={!reason.trim()}
            onClick={handleSubmit}
          >
            Reject deploy
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
