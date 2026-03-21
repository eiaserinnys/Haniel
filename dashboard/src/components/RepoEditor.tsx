// RepoEditor — dialog for adding/editing a repo config

import { useState } from 'react'
import type { RepoConfigInput } from '@/lib/types'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog'

interface RepoEditorProps {
  editName?: string
  editConfig?: RepoConfigInput
  onSave: (name: string, config: RepoConfigInput) => Promise<void>
  onCancel: () => void
}

export function RepoEditor({
  editName,
  editConfig,
  onSave,
  onCancel,
}: RepoEditorProps) {
  const isEdit = editName !== undefined

  const [name, setName] = useState(editName ?? '')
  const [url, setUrl] = useState(editConfig?.url ?? '')
  const [branch, setBranch] = useState(editConfig?.branch ?? 'main')
  const [path, setPath] = useState(editConfig?.path ?? '')
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  const handleSave = async () => {
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

    const config: RepoConfigInput = {
      url: url.trim(),
      branch: branch.trim() || 'main',
      path: path.trim(),
    }

    setSaving(true)
    setSaveError(null)
    try {
      await onSave(isEdit ? editName! : name.trim(), config)
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : String(e))
      setSaving(false)
    }
  }

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onCancel() }}>
      <DialogContent className="bg-zinc-900 border-zinc-700 text-zinc-100 max-w-lg">
        <DialogHeader>
          <DialogTitle className="text-zinc-100">
            {isEdit ? `Edit Repo: ${editName}` : 'Add New Repo'}
          </DialogTitle>
        </DialogHeader>

        <div className="flex flex-col gap-4 py-2">
          {/* Name */}
          <Field label="Name" required={!isEdit}>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={isEdit}
              placeholder="my-repo"
              className={inputClass(isEdit)}
            />
          </Field>

          {/* URL */}
          <Field label="URL" required>
            <input
              type="text"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://github.com/org/repo"
              className={inputClass(false)}
            />
          </Field>

          {/* Branch */}
          <Field label="Branch">
            <input
              type="text"
              value={branch}
              onChange={(e) => setBranch(e.target.value)}
              placeholder="main"
              className={inputClass(false)}
            />
          </Field>

          {/* Path */}
          <Field label="Local Path" required>
            <input
              type="text"
              value={path}
              onChange={(e) => setPath(e.target.value)}
              placeholder="./repos/my-repo"
              className={inputClass(false)}
            />
          </Field>

          {/* Error */}
          {saveError && (
            <div className="text-sm text-red-400 bg-red-900/20 border border-red-700/40 rounded px-3 py-2">
              {saveError}
            </div>
          )}
        </div>

        <DialogFooter className="gap-2">
          <button
            onClick={onCancel}
            disabled={saving}
            className="px-4 py-2 text-sm text-zinc-400 hover:text-zinc-200 transition-colors disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            onClick={handleSave}
            disabled={saving}
            className="px-4 py-2 text-sm bg-blue-600 hover:bg-blue-500 text-white rounded transition-colors disabled:opacity-50"
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function inputClass(disabled: boolean) {
  return [
    'w-full rounded px-3 py-1.5 text-sm bg-zinc-800 border border-zinc-600',
    'text-zinc-100 placeholder:text-zinc-500',
    'focus:outline-none focus:border-blue-500',
    disabled ? 'opacity-50 cursor-not-allowed' : '',
  ].join(' ')
}

interface FieldProps {
  label: string
  required?: boolean
  children: React.ReactNode
}

function Field({ label, required, children }: FieldProps) {
  return (
    <div className="flex flex-col gap-1.5">
      <label className="text-xs text-zinc-400">
        {label}
        {required && <span className="text-red-400 ml-1">*</span>}
      </label>
      {children}
    </div>
  )
}
