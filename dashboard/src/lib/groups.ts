// groups.ts — Group services and repos for the dashboard layout

import type { RunnerStatus, RepoStatus, ServiceStatus } from './types'

export type NamedRepo = { repoName: string; repo: RepoStatus }

export interface RepoServiceGroup {
  repoName: string
  repo: RepoStatus
  services: Array<{ name: string; service: ServiceStatus }>
}

export interface ServiceGroups {
  selfRepo: NamedRepo | null
  repoGroups: RepoServiceGroup[]
  standaloneServices: Array<{ name: string; service: ServiceStatus }>
  standaloneRepos: NamedRepo[]
}

export function groupServicesAndRepos(
  status: RunnerStatus,
  selfRepoName: string | undefined,
): ServiceGroups {
  const repoToServices = new Map<string, Array<{ name: string; service: ServiceStatus }>>()
  const standalone: Array<{ name: string; service: ServiceStatus }> = []

  for (const [name, service] of Object.entries(status.services)) {
    if (service.config.repo && status.repos[service.config.repo]) {
      // Repo exists in status.repos — map service to repo
      const list = repoToServices.get(service.config.repo) ?? []
      list.push({ name, service })
      repoToServices.set(service.config.repo, list)
    } else {
      // No repo, or repo not in status.repos — fallback to standalone
      standalone.push({ name, service })
    }
  }

  let selfRepo: NamedRepo | null = null
  const repoGroups: RepoServiceGroup[] = []
  const standaloneRepos: NamedRepo[] = []

  for (const [repoName, repo] of Object.entries(status.repos)) {
    if (selfRepoName && repoName === selfRepoName) {
      selfRepo = { repoName, repo }
    } else if (repoToServices.has(repoName)) {
      repoGroups.push({
        repoName,
        repo,
        services: repoToServices.get(repoName)!.sort((a, b) => a.name.localeCompare(b.name)),
      })
    } else {
      standaloneRepos.push({ repoName, repo })
    }
  }

  // Sort groups and standalone lists alphabetically
  repoGroups.sort((a, b) => a.repoName.localeCompare(b.repoName))
  standalone.sort((a, b) => a.name.localeCompare(b.name))
  standaloneRepos.sort((a, b) => a.repoName.localeCompare(b.repoName))

  return { selfRepo, repoGroups, standaloneServices: standalone, standaloneRepos }
}
