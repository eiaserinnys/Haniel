// ServiceEditor — dialog for adding/editing a service config

import { useState } from 'react'
import type { ServiceConfig, ServiceConfigInput } from '@/lib/types'
import { errorMessage } from '@/lib/utils'
import { Field } from './Field'
import { Icon } from './Icon'
import { Modal } from './Modal'
import { Notice } from './states'

interface ServiceEditorProps {
  editName?: string
  editConfig?: ServiceConfig
  availableRepos: string[]
  onSave: (name: string, config: ServiceConfigInput) => Promise<void>
  onCancel: () => void
}

export function ServiceEditor({ editName, editConfig, availableRepos, onSave, onCancel }: ServiceEditorProps) {
  const isEdit = editName !== undefined

  const [name, setName] = useState(editName ?? '')
  const [run, setRun] = useState(editConfig?.run ?? '')
  const [cwd, setCwd] = useState(editConfig?.cwd ?? '')
  const [repo, setRepo] = useState(editConfig?.repo ?? '')
  const [after, setAfter] = useState((editConfig?.after ?? []).join(', '))
  const [ready, setReady] = useState(editConfig?.ready ?? '')
  const [enabled, setEnabled] = useState(editConfig?.enabled ?? true)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  const handleSave = async () => {
    if (saving) return
    if (!run.trim()) {
      setSaveError('Run command is required.')
      return
    }
    if (!isEdit && !name.trim()) {
      setSaveError('Service name is required.')
      return
    }

    const config: ServiceConfigInput = {
      run: run.trim(),
      cwd: cwd.trim() || null,
      repo: repo || null,
      after: after.split(',').map((s) => s.trim()).filter(Boolean),
      ready: ready.trim() || null,
      enabled,
    }

    setSaving(true)
    setSaveError(null)
    try {
      await onSave(isEdit ? editName : name.trim(), config)
    } catch (e) {
      setSaveError(errorMessage(e))
      setSaving(false)
    }
  }

  return (
    <Modal
      title={isEdit ? 'Edit service' : 'Add service'}
      sub={isEdit ? <span className="mono">{editName}</span> : undefined}
      onClose={onCancel}
      onSubmit={handleSave}
      footer={
        <>
          <button type="button" className="btn btn-outline" onClick={onCancel} aria-disabled={saving || undefined}>
            Cancel
          </button>
          <button type="submit" className="btn btn-primary" aria-disabled={saving || undefined}>
            {saving ? 'Saving…' : 'Save'}
          </button>
        </>
      }
    >
      {saveError && <Notice summary="Couldn't save" detail={saveError} />}
      {!isEdit && (
        <Field label="Name" required>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="my-service" />
        </Field>
      )}
      <Field label="Run command" required>
        <input className="input mono-in" value={run} onChange={(e) => setRun(e.target.value)} placeholder="python -m myservice" />
      </Field>
      <Field label="Working directory">
        <input className="input mono-in" value={cwd} onChange={(e) => setCwd(e.target.value)} placeholder="./myrepo" />
      </Field>
      <Field label="Linked repo">
        <span className="select-wrap">
          <select className="select" value={repo} onChange={(e) => setRepo(e.target.value)}>
            <option value="">None</option>
            {availableRepos.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
          <Icon name="chev" small />
        </span>
      </Field>
      <Field label="Depends on" hint="comma-separated" hintIsText>
        <input
          className="input mono-in"
          value={after}
          onChange={(e) => setAfter(e.target.value)}
          placeholder="other-service, another-service"
        />
      </Field>
      <Field label="Ready condition" hint="port:3000 delay:5 log:started">
        <input className="input mono-in" value={ready} onChange={(e) => setReady(e.target.value)} placeholder="port:3000" />
      </Field>
      <label className="check-label">
        <input type="checkbox" className="check" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
        Enabled
      </label>
    </Modal>
  )
}
