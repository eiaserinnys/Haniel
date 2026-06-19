import { spawnSync } from 'node:child_process';

const command = process.platform === 'win32' ? 'vitest.cmd' : 'vitest';
const result = spawnSync(command, ['run', ...process.argv.slice(2)], {
  env: { ...process.env, NODE_ENV: 'test' },
  shell: true,
  stdio: 'inherit',
});

process.exit(result.status ?? 1);
