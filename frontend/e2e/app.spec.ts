import { expect, test } from '@playwright/test'

const sampleAsset = {
  asset_id: 'asset-castle',
  source_id: 'sample',
  source_display_name: 'Sample Assets',
  relative_path: 'maps/castle.png',
  filename: 'castle.png',
  stem: 'castle',
  extension: '.png',
  directory: 'maps',
  size_bytes: 2048,
  media_type: 'image',
  thumbnail_url: '',
}

function sampleDirectory(quality = 4, qualityOverride: number | null = null) {
  return {
    source_id: 'sample',
    path: 'maps',
    name: 'maps',
    depth: 0,
    asset_count: 1,
    matched_asset_count: 1,
    quality,
    quality_override: qualityOverride,
  }
}

test.beforeEach(async ({ page }) => {
  await page.route('**/api/sources', async (route) => {
    await route.fulfill({
      json: { sources: [{ id: 'sample', display_name: 'Sample Assets', priority: 0, root: 'sample-assets' }] },
    })
  })

  await page.route('**/api/directories?*', async (route) => {
    await route.fulfill({
      json: {
        directories: [sampleDirectory()],
      },
    })
  })

  await page.route('**/api/assets/asset-castle', async (route) => {
    await route.fulfill({
      json: {
        asset: sampleAsset,
        actions: {
          primary: { id: 'foundry', label: 'FoundryVTT', value: 'worlds/assets/maps/castle.png' },
          profiles: [],
          source_relative: sampleAsset.relative_path,
          absolute_path: '/tmp/sample-assets/maps/castle.png',
          open_url: '/api/assets/asset-castle/file',
          download_url: '/api/assets/asset-castle/download',
        },
      },
    })
  })

  await page.route('**/api/assets/asset-castle/file', async (route) => {
    await route.fulfill({
      contentType: 'image/gif',
      body: Buffer.from('R0lGODlhAQABAAAAACw=', 'base64'),
    })
  })

  await page.route('**/api/assets?*', async (route) => {
    await route.fulfill({ json: { total: 1, cards: [{ kind: 'asset', asset: sampleAsset }] } })
  })

  await page.route('**/api/search?*', async (route) => {
    await route.fulfill({ json: { total: 1, cards: [{ kind: 'asset', asset: sampleAsset }] } })
  })
})

test('searches through the real browser UI', async ({ page }) => {
  await page.goto('/')

  await expect(page.getByRole('button', { name: /castle\.png/i })).toBeVisible()
  await expect(page.getByRole('searchbox', { name: 'Search assets' })).toBeVisible()

  const searchRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return url.pathname === '/api/search' && url.searchParams.get('q') === 'castle'
  })

  await page.getByRole('searchbox', { name: 'Search assets' }).fill('castle')
  await searchRequest

  await expect(page.getByRole('button', { name: /castle\.png/i })).toBeVisible()
})

test('searches with the selected lane mode', async ({ page }) => {
  await page.goto('/')

  await expect(page.getByRole('button', { name: /castle\.png/i })).toBeVisible()
  await page.getByRole('searchbox', { name: 'Search assets' }).fill('castle')

  const domainSearchRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return (
      url.pathname === '/api/search' &&
      url.searchParams.get('q') === 'castle' &&
      url.searchParams.get('mode') === 'domain'
    )
  })

  await page.getByRole('tab', { name: 'Domain' }).click()
  await domainSearchRequest

  await expect(page.getByRole('tab', { name: 'Domain' })).toHaveAttribute('aria-selected', 'true')
})

test('filters asset requests by selected source', async ({ page }) => {
  await page.goto('/')

  await expect(page.getByRole('button', { name: /castle\.png/i })).toBeVisible()

  const filteredAssetsRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return url.pathname === '/api/assets' && url.searchParams.get('source_id') === 'sample'
  })

  await page.getByLabel('Source').selectOption('sample')
  await filteredAssetsRequest
})

test('copies the selected asset primary path', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: {
        writeText: (text: string) => {
          ;(window as unknown as { __copiedText?: string }).__copiedText = text
          return Promise.resolve()
        },
      },
    })
  })

  await page.goto('/')

  await page.getByRole('button', { name: /castle\.png/i }).click()
  await expect(page.getByLabel('Selected asset')).toContainText('maps/castle.png')

  await page.getByRole('button', { name: 'Copy' }).click()

  await expect(page.getByRole('button', { name: 'Copied' })).toBeVisible()
  await expect
    .poll(() => page.evaluate(() => (window as unknown as { __copiedText?: string }).__copiedText))
    .toBe('worlds/assets/maps/castle.png')
})

test('updates folder quality through the picker and refreshes ranked search results', async ({ page }) => {
  let directoryQuality = 4

  await page.route('**/api/directories?*', async (route) => {
    await route.fulfill({
      json: {
        directories: [
          sampleDirectory(directoryQuality, directoryQuality === 4 ? null : directoryQuality),
        ],
      },
    })
  })

  await page.route('**/api/folder-quality', async (route) => {
    const body = route.request().postDataJSON() as { quality: number }
    directoryQuality = body.quality
    await route.fulfill({
      json: { directory: sampleDirectory(directoryQuality, directoryQuality) },
    })
  })

  await page.goto('/')
  await expect(page.getByRole('button', { name: 'Quality 4 for maps' })).toBeVisible()

  const initialSearchRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return url.pathname === '/api/search' && url.searchParams.get('q') === 'castle'
  })
  await page.getByRole('searchbox', { name: 'Search assets' }).fill('castle')
  await initialSearchRequest

  const qualityUpdateRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return url.pathname === '/api/folder-quality' && request.method() === 'PUT'
  })
  const refreshedSearchRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return url.pathname === '/api/search' && url.searchParams.get('q') === 'castle'
  })

  await page.getByRole('button', { name: 'Quality 4 for maps' }).click()
  await page
    .getByRole('listbox', { name: 'Set quality for maps' })
    .getByRole('option', { name: '8' })
    .click()

  const request = await qualityUpdateRequest
  expect(request.postDataJSON()).toEqual({
    source_id: 'sample',
    directory: 'maps',
    quality: 8,
  })
  await refreshedSearchRequest
  await expect(page.getByRole('button', { name: 'Quality 8 for maps' })).toBeVisible()
})

test('adjusts the focused folder quality with keyboard shortcuts', async ({ page }) => {
  let directoryQuality = 4

  await page.route('**/api/directories?*', async (route) => {
    await route.fulfill({
      json: {
        directories: [
          sampleDirectory(directoryQuality, directoryQuality === 4 ? null : directoryQuality),
        ],
      },
    })
  })

  await page.route('**/api/folder-quality', async (route) => {
    const body = route.request().postDataJSON() as { quality: number }
    directoryQuality = body.quality
    await route.fulfill({
      json: { directory: sampleDirectory(directoryQuality, directoryQuality) },
    })
  })

  await page.goto('/')
  await page.getByRole('button', { name: 'Quality 4 for maps' }).focus()

  const qualityUpdateRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return url.pathname === '/api/folder-quality' && request.method() === 'PUT'
  })
  await page.keyboard.press('=')

  const request = await qualityUpdateRequest
  expect(request.postDataJSON()).toEqual({
    source_id: 'sample',
    directory: 'maps',
    quality: 5,
  })
  await expect(page.getByRole('button', { name: 'Quality 5 for maps' })).toBeVisible()
})
