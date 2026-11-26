import { useRef } from 'react';
import { Dialog } from '@base-ui/react/dialog';
import { plural } from '@/lib/deployInfo';
import type { Deploy } from '@/types';

interface ApproveConfirmModalProps {
  approvable: Deploy[];
  needsLook: Deploy[];
  onConfirm: () => void;
  onClose: () => void;
}

function Targets({ deploys }: { deploys: Deploy[] }) {
  return (
    <ul className="targets">
      {deploys.map(d => (
        <li key={d.deploy_id}>
          <span className="r-name">{d.repo}</span>
          <span className="r-node">{d.node_id}</span>
        </li>
      ))}
    </ul>
  );
}

// Asks before a batch approval restarts services, and names the selected
// requests that a batch never carries (their last deploy failed).
export function ApproveConfirmModal({ approvable, needsLook, onConfirm, onClose }: ApproveConfirmModalProps) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  const count = approvable.length;

  return (
    <Dialog.Root open onOpenChange={(open) => { if (!open) onClose(); }}>
      <Dialog.Portal>
        <Dialog.Backdrop className="scrim" />
        <Dialog.Popup className="dialog" initialFocus={cancelRef}>
          <div className="dialog-head">
            <Dialog.Title className="dialog-title">
              {count > 0 ? `Approve ${plural(count, 'deployment')}?` : 'Nothing to approve together'}
            </Dialog.Title>
            <Dialog.Description className="dialog-sub" render={<p />}>
              {count > 0 && <span>Services on those nodes restart.</span>}
            </Dialog.Description>
          </div>
          <div className="dialog-body">
            {count > 0 && <Targets deploys={approvable} />}
            {needsLook.length > 0 && (
              <>
                <p className="dialog-note">
                  {needsLook.length} need{needsLook.length === 1 ? 's' : ''} a look first — retry them one by one
                </p>
                <Targets deploys={needsLook} />
              </>
            )}
          </div>
          <div className="dialog-foot">
            <button ref={cancelRef} type="button" className="btn btn-outline" onClick={onClose}>Cancel</button>
            <button type="button" className="btn btn-primary" disabled={count === 0} onClick={onConfirm}>Approve</button>
          </div>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
