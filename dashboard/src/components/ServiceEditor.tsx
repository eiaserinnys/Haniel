// ServiceEditor — dialog for adding/editing a service config

import { useState } from 'react'
import type { ServiceConfig, ServiceConfigInput } from '@/lib/types'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog'

interface ServiceEditorProps {
  editName?: string
  editConfig?: ServiceConfig
  availableRepos: string[]
  availableServices: string[]
  onSave: (name: string, config: ServiceConfigInput) => Promise<void>
  onCancel: () => void
}

export function ServiceEditor({
  editName,
  editConfig,
  availableRepos,
  availableServices: _availableServices,
  onSave,
  onCancel,
}: ServiceEditorProps) {
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
    if (!run.trim()) {
      setSaveError('run 명령어는 필수입니다.')
      return
    }
    if (!isEdit && !name.trim()) {
      setSaveError('서비스 이름은 필수입니다.')
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
            {isEdit ? `서비스 편집: ${editName}` : '새 서비스 추가'}
          </DialogTitle>
        </DialogHeader>

        <div className="flex flex-col gap-4 py-2">
          {/* Name */}
          <Field label="이름" required={!isEdit}>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={isEdit}
              placeholder="my-service"
              className={inputClass(isEdit)}
            />
          </Field>

          {/* Run */}
          <Field label="run 명령어" required>
            <input
              type="text"
              value={run}
              onChange={(e) => setRun(e.target.value)}
              placeholder="python -m myservice"
              className={inputClass(false)}
            />
          </Field>

          {/* CWD */}
          <Field label="작업 디렉토리 (cwd)">
            <input
              type="text"
              value={cwd}
              onChange={(e) => setCwd(e.target.value)}
              placeholder="./myrepo"
              className={inputClass(false)}
            />
          </Field>

          {/* Repo */}
          <Field label="연결 리포">
            <select
              value={repo}
              onChange={(e) => setRepo(e.target.value)}
              className={selectClass}
            >
              <option value="">없음</option>
              {availableRepos.map((r) => (
                <option key={r} value={r}>{r}</option>
              ))}
            </select>
          </Field>

          {/* After */}
          <Field label="의존 서비스 (after)" hint="쉼표로 구분">
            <input
              type="text"
              value={after}
              onChange={(e) => setAfter(e.target.value)}
              placeholder="other-service, another-service"
              className={inputClass(false)}
            />
          </Field>

          {/* Ready */}
          <Field label="준비 조건 (ready)" hint="port:3000 / delay:5 / log:started">
            <input
              type="text"
              value={ready}
              onChange={(e) => setReady(e.target.value)}
              placeholder="port:3000"
              className={inputClass(false)}
            />
          </Field>

          {/* Enabled */}
          <label className="flex items-center gap-3 cursor-pointer">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
              className="w-4 h-4 rounded accent-blue-500"
            />
            <span className="text-sm text-zinc-300">활성화 (enabled)</span>
          </label>

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
            취소
          </button>
          <button
            onClick={handleSave}
            disabled={saving}
            className="px-4 py-2 text-sm bg-blue-600 hover:bg-blue-500 text-white rounded transition-colors disabled:opacity-50"
          >
            {saving ? '저장 중…' : '저장'}
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

const selectClass =
  'w-full rounded px-3 py-1.5 text-sm bg-zinc-800 border border-zinc-600 ' +
  'text-zinc-100 focus:outline-none focus:border-blue-500'

interface FieldProps {
  label: string
  required?: boolean
  hint?: string
  children: React.ReactNode
}

function Field({ label, required, hint, children }: FieldProps) {
  return (
    <div className="flex flex-col gap-1.5">
      <label className="text-xs text-zinc-400">
        {label}
        {required && <span className="text-red-400 ml-1">*</span>}
        {hint && <span className="ml-1 text-zinc-600">({hint})</span>}
      </label>
      {children}
    </div>
  )
}
