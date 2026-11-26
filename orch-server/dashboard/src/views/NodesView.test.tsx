import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { NodesView } from './NodesView';
import type { OrchestratorNode } from '@/types';

function node(overrides: Partial<OrchestratorNode> = {}): OrchestratorNode {
  return {
    node_id: 'node-1',
    hostname: 'host',
    os: 'linux',
    arch: 'x86_64',
    haniel_version: 'abc1234',
    connected: 1,
    last_seen: new Date().toISOString(),
    created_at: new Date().toISOString(),
    services: [],
    ...overrides,
  };
}

function open(name: string) {
  fireEvent.click(screen.getByRole('button', { name: new RegExp(name), expanded: false }));
}

describe('NodesView', () => {
  it('lists what the node serves, when it last fetched, and how often it polls', () => {
    const fetched = new Date(Date.now() - 2 * 60_000).toISOString();
    render(
      <NodesView
        nodes={[node({
          poll_interval: 60,
          repo_fetches: [{ repo: 'haniel', last_fetch: fetched }, { repo: 'lore-search', last_fetch: null }],
          serving: [{
            repo: 'haniel', branch: 'main', target_head: 'e9416d7aabbccddeeff00112233445566778899',
            deployed_at: new Date().toISOString(), deploy_id: 'node-1:haniel:main:e9416d7',
          }],
        })]}
      />,
    );
    open('node-1');
    const ledger = screen.getByRole('group', { name: 'Repositories' });
    expect(within(ledger).getByText('Last successful deploy per repository', { exact: false })).toBeInTheDocument();
    expect(within(ledger).getByText('60s')).toBeInTheDocument();
    expect(ledger.textContent).toContain('Polls every 60s');
    const haniel = within(ledger).getByText('haniel').closest('li') as HTMLElement;
    expect(within(haniel).getByText('e9416d7a')).toHaveClass('mono');
    expect(haniel.textContent).toMatch(/serving e9416d7a · \d\d:\d\d/);
    expect(haniel.textContent).toContain('checked 2m ago');
    const lore = within(ledger).getByText('lore-search').closest('li') as HTMLElement;
    expect(lore.textContent).toContain('No successful deploy recorded');
    expect(lore.textContent).not.toContain('checked');
  });

  it('says when no successful deploy is recorded and leaves the polling line out for an old node', () => {
    render(<NodesView nodes={[node({ poll_interval: null, repo_fetches: null, serving: [] })]} />);
    open('node-1');
    const ledger = screen.getByRole('group', { name: 'Repositories' });
    expect(ledger.textContent).toContain('No successful deploy recorded');
    expect(ledger.textContent).not.toContain('Polls every');
  });

  it('shows a disconnected node as restarting while its self-update deploys', () => {
    render(
      <NodesView
        nodes={[
          node({ node_id: 'restarting-node', connected: 0, self_update_deploying: true, self_update_deploy_id: 'x' }),
          node({ node_id: 'lost-node', connected: 0, self_update_deploying: false }),
          node({ node_id: 'live-node' }),
        ]}
      />,
    );
    const row = (id: string) => screen.getByText(id).closest('.row') as HTMLElement;
    expect(within(row('restarting-node')).getByText('Restarting (self-update)')).toHaveClass('st-busy');
    expect(within(row('restarting-node')).queryByText('Disconnected')).not.toBeInTheDocument();
    expect(within(row('lost-node')).getByText('Disconnected')).toHaveClass('st-fail');
    expect(within(row('live-node')).getByText('Connected')).toHaveClass('st-ok');
  });
});
