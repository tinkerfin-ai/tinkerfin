import { defineConfig, devices } from '@playwright/test'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const artifactRoot = join(tmpdir(), 'tinkerfin-studio-playwright')

export default defineConfig({
  testDir: './tests/browser',
  fullyParallel: false,
  timeout: 30_000,
  expect: { timeout: 5_000 },
  outputDir: join(artifactRoot, 'test-results'),
  reporter: [['list'], ['html', { open: 'never', outputFolder: join(artifactRoot, 'report') }]],
  use: {
    baseURL: 'http://127.0.0.1:4173',
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'pnpm build && pnpm preview --host 127.0.0.1 --port 4173',
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
  projects: [{
    name: 'chromium',
    use: { ...devices['Desktop Chrome'] },
  }],
})
