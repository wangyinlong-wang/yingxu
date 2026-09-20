import { defineConfig } from '@playwright/test'
export default defineConfig({
  testDir:'./tests/e2e',
  timeout:20_000,
  workers:1,
  webServer:{command:'pnpm dev',url:'http://127.0.0.1:5173',reuseExistingServer:!process.env.CI,timeout:120_000},
  use:{ baseURL:'http://127.0.0.1:5173', browserName:'chromium', launchOptions:process.env.PLAYWRIGHT_EXECUTABLE_PATH ? { executablePath:process.env.PLAYWRIGHT_EXECUTABLE_PATH } : undefined }
})
