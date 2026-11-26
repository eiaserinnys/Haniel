// RepoEditor — dialog for adding/editing a repo config

import { useState } from 'react'
import type { RepoConfigInput } from '@/lib/types'
import { errorMessage } from '@/lib/utils'
import { Field } from './Field'
import { Modal } from './Modal'
import { Notice } from './states'

interface RepoEditorProps {
  editName?: string
  editConfig?: RepoConfigInput
  onSave: (name: string, config: RepoConfigInput) => Promise<void>
  onCancel: () => void
}

export function RepoEditor({ editName, editConfig, onSave, onCancel }: RepoEditorProps) {
  const isEdit = editName !== undefined

  const [name, setName] = useState(editName ?? '')
  const [url, setUrl] = useState(editConfig?.url ?? '')
  const [branch, setBranch] = useState(editConfig?.branch ?? 'main')
  const [path, setPath] = useState(editConfig?.path ?? '')
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  const handleSave = async () => {
    if (saving) return
    if (!url.trim()) {
      setSaveError('URL is required.')
      return
    }
    if (!path.trim()) {
      setSaveError('Path is required.')
      return
    }
    if (!isEdit && !name.trim()) {
      setSaveError('Repo name is required.')
      return
    }

    const config: RepoConfigInput = { url: url.trim(), branch: branch.trim() || 'main', path: path.trim() }

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
      title={isEdit ? 'Edit repo' : 'Add repo'}
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
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="my-repo" />
        </Field>
      )}
      <Field label="URL" required>
        <input className="input mono-in" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://github.com/org/repo" />
      </Field>
      <Field label="Branch">
        <input className="input mono-in" value={branch} onChange={(e) => setBranch(e.target.value)} placeholder="main" />
      </Field>
      <Field label="Local path" required>
        <input className="input mono-in" value={path} onChange={(e) => setPath(e.target.value)} placeholder="./repos/my-repo" />
      </Field>
    </Modal>
  )
}
