import { expect, test, type Page } from '@playwright/test'

type FilterRuleState = 'whitelist' | 'blacklist'

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

function actionsFor(asset: typeof sampleAsset, absolutePath: string) {
  const fileUrl = `/api/assets/${asset.asset_id}/file`
  return {
    open: { id: 'open', label: 'Open', url: fileUrl },
    copy: { id: 'copy', label: 'Copy', url: fileUrl, enabled: true },
    copy_thumbnail: {
      id: 'copy_thumbnail',
      label: 'Copy Thumbnail',
      url: `/api/assets/${asset.asset_id}/thumb?v=1-1`,
      enabled: true,
    },
    download: { id: 'download', label: 'Download', url: `${fileUrl}?download=1` },
    copy_path: { id: 'copy_path', label: 'Copy path', value: absolutePath },
    copy_foundryvtt_path: {
      id: 'copy_foundryvtt_path',
      label: 'Copy FoundryVTT path',
      value: `library/Sample Assets/${asset.relative_path}`,
    },
  }
}

function sampleDirectory(
  quality = 4,
  qualityOverride: number | null = null,
  selectionState: FilterRuleState | null = null,
  selectionEffective = true,
  profileDirty = false,
  hasDescendantWhitelist = false,
  hasDescendantBlacklist = false
) {
  return {
    id: 1,
    source_id: 'sample',
    path: 'maps',
    name: 'maps',
    depth: 1,
    asset_count: 1,
    matched_asset_count: 1,
    included_asset_count: 1,
    quality,
    quality_override: qualityOverride,
    selection_state: selectionState,
    selection_effective: selectionEffective,
    profile_dirty: profileDirty,
    has_descendant_whitelist: hasDescendantWhitelist,
    has_descendant_blacklist: hasDescendantBlacklist,
    file_browser_url: 'https://filebrowser.example/files/DND/maps',
  }
}

function sampleRootDirectory() {
  return {
    id: 0,
    source_id: 'sample',
    path: '',
    name: 'Sample Assets',
    depth: 0,
    asset_count: 1,
    matched_asset_count: 1,
    included_asset_count: 1,
    quality: 5,
    quality_override: null,
    selection_state: null,
    selection_effective: true,
    profile_dirty: false,
    has_descendant_whitelist: false,
    has_descendant_blacklist: false,
    file_browser_url: 'https://filebrowser.example/files/DND',
  }
}

function directoryState(directory: ReturnType<typeof sampleRootDirectory>) {
  return [
    directory.id,
    directory.matched_asset_count,
    directory.included_asset_count,
    directory.quality,
    directory.quality_override,
    directory.selection_state,
    directory.selection_effective,
    directory.profile_dirty,
    directory.has_descendant_whitelist,
    directory.has_descendant_blacklist,
  ]
}

function profileSession(dirtyScoring = false, dirtySelection = false) {
  return {
    search_session: 'test-session',
    active_profile_id: 'default',
    active_profile_name: 'Default',
    active_profile_kind: 'default',
    dirty_scoring: dirtyScoring,
    dirty_selection: dirtySelection,
    dirty: dirtyScoring || dirtySelection,
  }
}

function svgImage(width: number, height: number, color = '#1e7a5c') {
  return Buffer.from(
    `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}"><rect width="${width}" height="${height}" fill="${color}"/></svg>`
  )
}

function expectProfileSettingRequest(
  request: { postDataJSON: () => unknown },
  expected: { source_id: string; directory: string; quality_override?: number | null; selection_state?: FilterRuleState | null }
) {
  expect(request.postDataJSON()).toEqual({
    search_session: expect.any(String),
    ...expected,
  })
}

function waitForProfileSetting(
  page: Page,
  expected: { source_id?: string; directory?: string; quality_override?: number | null; selection_state?: FilterRuleState | null }
) {
  return page.waitForRequest((request) => {
    const url = new URL(request.url())
    if (url.pathname !== '/api/search-profile/settings' || request.method() !== 'PATCH') return false
    const body = request.postDataJSON() as Record<string, unknown>
    return Object.entries(expected).every(([key, value]) => body[key] === value)
  })
}

function expectSearchSession(request: { url: () => string }, searchSession = 'test-session') {
  const url = new URL(request.url())
  expect(url.searchParams.getAll('search_session')).toEqual([searchSession])
}

async function revealSampleFolder(page: Page) {
  await page.getByRole('button', { name: /castle\.png/i }).click()
  await expect(page.getByRole('group', { name: 'maps folder' })).toBeVisible()
}

async function gridColumnCount(page: Page) {
  return page.locator('.tile-grid').evaluate((grid) => {
    const columns = window.getComputedStyle(grid).gridTemplateColumns
    if (!columns || columns === 'none') return 1
    return columns.split(' ').filter(Boolean).length
  })
}

async function tileThumbSize(page: Page) {
  const box = await page.locator('.tile-thumb').first().boundingBox()
  if (!box) throw new Error('tile thumb is missing')
  return box
}

function sampleAssetAt(index: number) {
  return {
    ...sampleAsset,
    asset_id: `asset-${index}`,
    relative_path: `maps/asset-${index}.png`,
    filename: `asset-${index}.png`,
    stem: `asset-${index}`,
  }
}

async function routeSearchQuery(page: Page, assets: ReturnType<typeof sampleAssetAt>[]) {
  const cards = assets.map((asset) => ({ kind: 'asset' as const, asset }))
  const root = {
    ...sampleRootDirectory(),
    asset_count: assets.length,
    matched_asset_count: assets.length,
    included_asset_count: assets.length,
  }
  const maps = {
    ...sampleDirectory(),
    asset_count: assets.length,
    matched_asset_count: assets.length,
    included_asset_count: assets.length,
  }

  await page.route('**/api/search/query', async (route) => {
    const body = route.request().postDataJSON() as {
      result_shape?: string
      directory_state_only?: boolean
      limit?: number
      offset?: number
    }
    if (body.result_shape === 'directories') {
      await route.fulfill({
        json: body.directory_state_only ? { directory_states: [root, maps].map(directoryState) } : { directories: [root, maps] },
      })
      return
    }
    const offset = body.offset ?? 0
    const limit = body.limit ?? assets.length
    const pageCards = cards.slice(offset, offset + limit)
    const nextOffset = offset + pageCards.length
    await route.fulfill({
      json: {
        total: assets.length,
        cards: pageCards,
        has_more: nextOffset < assets.length,
        next_offset: nextOffset,
      },
    })
  })
}

async function disableAutomaticInfiniteScroll(page: Page) {
  await page.addInitScript(() => {
    class ManualIntersectionObserver {
      constructor(_callback: IntersectionObserverCallback) {}
      observe() {}
      unobserve() {}
      disconnect() {}
      takeRecords() {
        return []
      }
    }

    Object.defineProperty(window, 'IntersectionObserver', {
      configurable: true,
      writable: true,
      value: ManualIntersectionObserver,
    })
  })
}

async function captureWindowOpen(page: Page) {
  await page.addInitScript(() => {
    window.open = ((url?: string | URL) => {
      const testWindow = window as Window & typeof globalThis & { __openedUrl?: string }
      testWindow.__openedUrl = url?.toString()
      return null
    }) as typeof window.open
  })
}

test.beforeEach(async ({ page }) => {
  let directoryQuality = 4
  let directorySelectionState: FilterRuleState | null = null

  await page.route('**/api/search-profiles?*', async (route) => {
    await route.fulfill({
      json: {
        profiles: [
          { id: 'none', name: 'None', kind: 'none', version: 1, sort_order: 0 },
          { id: 'default', name: 'Default', kind: 'default', version: 1, sort_order: 1 },
        ],
        session: profileSession(),
        settings: [],
      },
    })
  })

  await page.route('**/api/search-profile/settings', async (route) => {
    const body = route.request().postDataJSON() as {
      directory?: string
      quality_override?: number | null
      selection_state?: FilterRuleState | null
    }
    if (body.directory === 'maps') {
      if ('quality_override' in body) directoryQuality = body.quality_override ?? 5
      if ('selection_state' in body) directorySelectionState = body.selection_state ?? null
    }
    await route.fulfill({
      json: {
        session: profileSession('quality_override' in body, 'selection_state' in body),
      },
    })
  })

  await page.route('**/api/assets/asset-castle', async (route) => {
    await route.fulfill({
      json: {
        asset: sampleAsset,
        actions: actionsFor(sampleAsset, '/tmp/sample-assets/maps/castle.png'),
      },
    })
  })

  await page.route('**/api/assets/asset-castle/file', async (route) => {
    await route.fulfill({
      contentType: 'image/gif',
      body: Buffer.from('R0lGODlhAQABAAAAACw=', 'base64'),
    })
  })

  await page.route('**/api/search-with-directory-counts?*', async (route) => {
    const url = new URL(route.request().url())
    const directories = [
      sampleRootDirectory(),
      sampleDirectory(directoryQuality, directoryQuality === 4 ? null : directoryQuality, directorySelectionState),
    ]
    await route.fulfill({
      json: {
        total: 1,
        cards: [{ kind: 'asset', asset: sampleAsset }],
        ...(url.searchParams.get('directory_state_only') === 'true'
          ? { directory_states: directories.map(directoryState) }
          : { directories }),
      },
    })
  })

  await page.route('**/api/search?*', async (route) => {
    await route.fulfill({ json: { total: 1, cards: [{ kind: 'asset', asset: sampleAsset }] } })
  })

  await page.route('**/api/index-jobs/current', async (route) => {
    await route.fulfill({ json: { job: null } })
  })
})

test('restores an active index job as a compact spinner with expandable details', async ({ page }) => {
  await routeSearchQuery(page, [sampleAsset])
  const startedAt = Date.now() / 1000 - 12
  const runningJob = {
    id: 'job-1',
    status: 'running',
    scope: { kind: 'folder', source_id: 'sample', directory: 'maps' },
    phase: 'indexing',
    detail: 'Reading maps/keep.png',
    scanned_files: 18,
    new_assets: 3,
    written_assets: 3,
    generated_thumbnails: 2,
    failed_thumbnails: 0,
    current_relative_path: 'maps/keep.png',
    current_action: 'reading',
    elapsed_seconds: 12,
    error: null,
    started_at: startedAt,
    finished_at: null,
  }

  await page.route('**/api/index-jobs/current', async (route) => {
    await route.fulfill({ json: { job: runningJob } })
  })
  await page.route('**/api/index-jobs/job-1', async (route) => {
    await route.fulfill({
      json: {
        job: {
          ...runningJob,
          phase: 'rebuilding-search',
          detail: 'Building lexical search index',
          scanned_files: 24,
          new_assets: 4,
          written_assets: 4,
          elapsed_seconds: 15,
        },
      },
    })
  })

  await page.goto('/')

  const jobButton = page.getByRole('button', { name: 'Index job running' })
  await expect(jobButton).toBeVisible()
  await expect(page.locator('.status')).toContainText('1 card')
  await expect(page.locator('.status')).not.toContainText('Scanned')

  await jobButton.click()
  const panel = page.getByRole('dialog', { name: 'Index jobs' })
  await expect(panel).toBeVisible()
  await expect(panel).toContainText('sample / maps')
  await expect(panel).toContainText('18')
  await expect(panel).toContainText('Reading maps/keep.png')

  await expect(panel).toContainText('Preparing search index')
  await expect(panel).toContainText('Building lexical search index')
  await expect(jobButton).toBeVisible()
})

test('keeps polling an active index job after a transient status failure', async ({ page }) => {
  await routeSearchQuery(page, [sampleAsset])
  const runningJob = {
    id: 'job-1',
    status: 'running',
    scope: { kind: 'folder', source_id: 'sample', directory: 'maps' },
    phase: 'indexing',
    detail: 'Reading maps/keep.png',
    scanned_files: 18,
    new_assets: 3,
    written_assets: 3,
    generated_thumbnails: 2,
    failed_thumbnails: 0,
    current_source_id: 'sample',
    current_relative_path: 'maps/keep.png',
    current_action: 'reading',
    elapsed_seconds: 12,
    error: null,
    started_at: Date.now() / 1000 - 12,
    finished_at: null,
  }
  let pollRequests = 0

  await page.route('**/api/index-jobs/current', async (route) => {
    await route.fulfill({ json: { job: runningJob } })
  })
  await page.route('**/api/index-jobs/job-1', async (route) => {
    pollRequests += 1
    if (pollRequests === 1) {
      await route.fulfill({ status: 503, json: { detail: 'temporarily unavailable' } })
      return
    }
    await route.fulfill({ json: { job: { ...runningJob, detail: 'Reading maps/tower.png' } } })
  })

  await page.goto('/')

  await expect.poll(() => pollRequests).toBeGreaterThanOrEqual(2)
  const jobButton = page.getByRole('button', { name: 'Index job running' })
  await expect(jobButton).toBeVisible()
  await jobButton.click()
  await expect(page.getByRole('dialog', { name: 'Index jobs' })).toContainText('Reading maps/tower.png')
})

test('searches through the real browser UI', async ({ page }) => {
  await page.goto('/')

  await expect(page.getByRole('button', { name: /castle\.png/i })).toBeVisible()
  await expect(page.getByRole('searchbox', { name: 'Search assets' })).toBeVisible()
  await expect(page.getByLabel('Source')).toHaveCount(0)
  await expect(page.getByRole('group', { name: 'Sample Assets folder' })).toHaveClass(/source-root/)
  await expect(page.getByRole('group', { name: 'Sample Assets folder' }).locator('.folder-main > svg')).toHaveCount(1)
  await expect(page.getByText('Preview')).toHaveCount(0)

  const searchRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return url.pathname === '/api/search' && url.searchParams.get('q') === 'castle'
  })

  await page.getByRole('searchbox', { name: 'Search assets' }).fill('castle')
  await searchRequest

  await expect(page.getByRole('button', { name: /castle\.png/i })).toBeVisible()
})

test('middle-clicking the selected image preview opens the asset file in a new tab', async ({ page }) => {
  await captureWindowOpen(page)
  await page.goto('/')

  await page.getByRole('button', { name: /castle\.png/i }).click()
  const previewImage = page.getByLabel('Selected asset').locator('.preview img')
  await expect(previewImage).toBeVisible()

  await previewImage.click({ button: 'middle' })

  await expect
    .poll(() => page.evaluate(() => (window as Window & typeof globalThis & { __openedUrl?: string }).__openedUrl))
    .toBe('/api/assets/asset-castle/file')
})

test('middle-clicking a folder row opens its configured file browser URL in a new tab', async ({ page }) => {
  await captureWindowOpen(page)
  await page.goto('/')
  await revealSampleFolder(page)

  await page.getByRole('group', { name: 'maps folder' }).getByRole('button', { name: 'maps', exact: true }).click({
    button: 'middle',
  })

  await expect
    .poll(() => page.evaluate(() => (window as Window & typeof globalThis & { __openedUrl?: string }).__openedUrl))
    .toBe('https://filebrowser.example/files/DND/maps')
})

test('changes the number of visible grid columns from the toolbar', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('button', { name: /castle\.png/i })).toBeVisible()

  await expect.poll(() => gridColumnCount(page)).toBe(12)
  await expect(page.getByLabel('12 cards per row')).toBeVisible()
  await expect(page.locator('.grid-width-controls')).not.toContainText('px')
  const initialThumb = await tileThumbSize(page)
  expect(Math.abs(initialThumb.width - initialThumb.height)).toBeLessThanOrEqual(1)

  await page.getByRole('button', { name: 'Show more cards in grid' }).click()
  await expect.poll(() => gridColumnCount(page)).toBe(13)
  await expect(page.getByLabel('13 cards per row')).toBeVisible()
  const denserThumb = await tileThumbSize(page)
  expect(denserThumb.width).toBeLessThan(initialThumb.width)
  expect(Math.abs(denserThumb.width - denserThumb.height)).toBeLessThanOrEqual(1)

  await page.getByRole('button', { name: 'Show fewer cards in grid' }).click()
  await expect.poll(() => gridColumnCount(page)).toBe(12)

  for (let index = 12; index < 15; index += 1) {
    await page.getByRole('button', { name: 'Show more cards in grid' }).click()
  }
  await expect.poll(() => gridColumnCount(page)).toBe(15)
  await expect(page.getByLabel('15 cards per row')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Show more cards in grid' })).toBeDisabled()

  for (let index = 15; index > 1; index -= 1) {
    await page.getByRole('button', { name: 'Show fewer cards in grid' }).click()
  }
  await expect.poll(() => gridColumnCount(page)).toBe(1)
  await expect(page.getByLabel('1 card per row')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Show fewer cards in grid' })).toBeDisabled()

  await page.reload()
  await expect(page.getByRole('button', { name: /castle\.png/i })).toBeVisible()
  await expect.poll(() => gridColumnCount(page)).toBe(12)
  await expect(page.getByLabel('12 cards per row')).toBeVisible()
})

test('wheels over cards to scroll normally and ctrl-wheel grid width', async ({ page }) => {
  const assets = Array.from({ length: 240 }, (_, index) => sampleAssetAt(index))
  const cards = assets.map((asset) => ({ kind: 'asset', asset }))
  const root = { ...sampleRootDirectory(), asset_count: assets.length, matched_asset_count: assets.length, included_asset_count: assets.length }
  const maps = { ...sampleDirectory(), asset_count: assets.length, matched_asset_count: assets.length, included_asset_count: assets.length }

  await page.unroute('**/api/search?*')
  await page.unroute('**/api/search-with-directory-counts?*')
  await page.route('**/api/search?*', async (route) => {
    await route.fulfill({ json: { total: assets.length, cards, has_more: false, next_offset: assets.length } })
  })
  await page.route('**/api/search-with-directory-counts?*', async (route) => {
    await route.fulfill({ json: { directories: [root, maps] } })
  })

  await page.goto('/')
  await expect(page.getByRole('button', { name: /asset-0\.png/i })).toBeVisible()
  const grid = page.locator('.tile-grid')
  await expect.poll(() => grid.evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(true)

  await page.getByRole('button', { name: /asset-0\.png/i }).hover()
  const initialScrollTop = await grid.evaluate((element) => element.scrollTop)
  await page.mouse.wheel(0, 520)

  await expect.poll(() => grid.evaluate((element) => element.scrollTop)).toBeGreaterThan(initialScrollTop)

  await page.keyboard.down('Control')
  await page.mouse.wheel(0, 520)
  await page.keyboard.up('Control')
  await expect.poll(() => gridColumnCount(page)).toBe(13)
  await expect(page.getByLabel('13 cards per row')).toBeVisible()

  await page.keyboard.down('Control')
  await page.mouse.wheel(0, -520)
  await page.keyboard.up('Control')
  await expect.poll(() => gridColumnCount(page)).toBe(12)
  await expect(page.getByLabel('12 cards per row')).toBeVisible()
})

test('shows batch tag controls in the toolbar only while assets are selected', async ({ page }) => {
  const assets = Array.from({ length: 240 }, (_, index) => sampleAssetAt(index))

  await routeSearchQuery(page, assets)

  await page.goto('/')
  await expect(page.getByRole('group', { name: 'Batch tagging' })).toHaveCount(0)
  await expect(page.getByRole('searchbox', { name: 'Search assets' })).toBeVisible()
  await expect(page.getByRole('tab', { name: 'Exact' })).toBeVisible()

  await page.getByRole('button', { name: /asset-0\.png/i }).click({ modifiers: ['Control'] })

  const grid = page.locator('.tile-grid')
  const batchTagging = page.getByRole('group', { name: 'Batch tagging' })
  await expect(batchTagging).toContainText('1 selected')
  await expect(page.getByRole('searchbox', { name: 'Search assets' })).toHaveCount(0)
  await expect(page.getByRole('tab', { name: 'Exact' })).toHaveCount(0)
  await expect(batchTagging.getByRole('button', { name: 'Folder' })).toHaveCount(0)
  await expect(batchTagging.getByRole('button', { name: 'All results' })).toBeVisible()
  await expect.poll(() => grid.evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(true)

  const initialGridBox = await grid.boundingBox()
  const initialTagBox = await batchTagging.boundingBox()
  if (!initialGridBox || !initialTagBox) throw new Error('batch tag controls are missing')
  expect(initialTagBox.y + initialTagBox.height).toBeLessThanOrEqual(initialGridBox.y + 2)

  await grid.evaluate((element) => {
    element.scrollTop = element.scrollHeight
  })

  await expect.poll(() => grid.evaluate((element) => element.scrollTop)).toBeGreaterThan(0)
  await expect(page.getByRole('button', { name: /asset-0\.png/i })).not.toBeInViewport()
  await expect(batchTagging).toBeInViewport()

  const scrolledTagBox = await batchTagging.boundingBox()
  if (!scrolledTagBox) throw new Error('batch tag controls are missing after scroll')
  expect(Math.abs(scrolledTagBox.y - initialTagBox.y)).toBeLessThanOrEqual(2)
  expect(scrolledTagBox.y + scrolledTagBox.height).toBeLessThanOrEqual(initialGridBox.y + 2)
})

test('selects all visible assets with ctrl-a', async ({ page }) => {
  const assets = Array.from({ length: 3 }, (_, index) => sampleAssetAt(index))

  await routeSearchQuery(page, assets)

  await page.goto('/')
  await expect(page.getByRole('button', { name: /asset-0\.png/i })).toBeVisible()

  await page.keyboard.press('Control+A')

  await expect(page.getByRole('group', { name: 'Batch tagging' })).toContainText('3 selected')
  await expect(page.locator('.asset-tile.multi-selected')).toHaveCount(3)
})

test('keeps the result scroll position after tagging selected assets', async ({ page }) => {
  const assets = Array.from({ length: 240 }, (_, index) => sampleAssetAt(index))

  await routeSearchQuery(page, assets)
  await page.route('**/api/tags', async (route) => {
    await route.fulfill({ json: { tags: [{ slug: 'favorite', label: 'favorite' }] } })
  })
  await page.route('**/api/tags/batch', async (route) => {
    await route.fulfill({ json: { matched: 1, tagged: 1, failed: [] } })
  })

  await page.goto('/')
  await expect(page.getByRole('button', { name: /asset-0\.png/i })).toBeVisible()
  await page.getByRole('button', { name: 'Load more' }).click()
  await expect(page.getByRole('button', { name: /asset-130\.png/i })).toBeVisible()

  const grid = page.locator('.tile-grid')
  await grid.evaluate((element) => {
    element.scrollTop = 1600
  })
  await expect.poll(() => grid.evaluate((element) => element.scrollTop)).toBeGreaterThan(500)
  const scrollBeforeTagging = await grid.evaluate((element) => element.scrollTop)

  await page.getByRole('button', { name: /asset-130\.png/i }).click({ modifiers: ['Control'] })
  await page.getByRole('textbox', { name: 'Batch tag' }).fill('favorite')
  await page.getByRole('button', { name: 'Apply' }).click()

  await expect(page.getByRole('group', { name: 'Batch tagging' })).toContainText('Updated 1')
  await expect.poll(() => grid.evaluate((element) => element.scrollTop)).toBeGreaterThan(scrollBeforeTagging - 20)
})

test('filters results from the tags sidebar tab', async ({ page }) => {
  await page.route('**/api/tags', async (route) => {
    await route.fulfill({
      json: {
        tags: [
          { slug: 'favorite', label: 'favorite' },
          { slug: 'hidden', label: 'hidden' },
          { slug: 'npc', label: 'NPC' },
        ],
      },
    })
  })

  await page.route('**/api/search/query', async (route) => {
    const body = route.request().postDataJSON() as {
      result_shape?: string
      directory_state_only?: boolean
      where?: unknown
    }
    if (body.result_shape === 'directories') {
      await route.fulfill({
        json: body.directory_state_only
          ? { directory_states: [sampleRootDirectory(), sampleDirectory()].map(directoryState) }
          : { directories: [sampleRootDirectory(), sampleDirectory()] },
      })
      return
    }
    await route.fulfill({
      json: {
        total: 1,
        cards: [{ kind: 'asset', asset: { ...sampleAsset, tags: [{ slug: 'favorite', label: 'favorite' }] } }],
        has_more: false,
        next_offset: 1,
      },
    })
  })

  await page.goto('/')
  await expect(page.getByRole('button', { name: /castle\.png/i })).toBeVisible()

  await page.getByRole('tab', { name: 'Tags' }).click()
  await expect(page.getByRole('tabpanel', { name: 'Tags' })).toBeVisible()

  const includeRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    if (url.pathname !== '/api/search/query' || request.method() !== 'POST') return false
    const body = request.postDataJSON() as { where?: unknown; result_shape?: string }
    return body.result_shape !== 'directories' && JSON.stringify(body.where) === JSON.stringify({ tag: 'favorite' })
  })
  await page.getByRole('button', { name: 'Include favorite' }).click()
  await includeRequest
  await expect(page.getByRole('button', { name: 'Include favorite' })).toHaveClass(/active/)

  const excludeRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    if (url.pathname !== '/api/search/query' || request.method() !== 'POST') return false
    const body = request.postDataJSON() as { where?: unknown; result_shape?: string }
    return (
      body.result_shape !== 'directories' &&
      JSON.stringify(body.where) ===
        JSON.stringify({
          and: [{ tag: 'favorite' }, { not: { tag: 'hidden' } }],
        })
    )
  })
  await page.getByRole('button', { name: 'Exclude hidden' }).click()
  await excludeRequest
  await expect(page.getByRole('button', { name: 'Exclude hidden' })).toHaveClass(/active/)

  const secondIncludeRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    if (url.pathname !== '/api/search/query' || request.method() !== 'POST') return false
    const body = request.postDataJSON() as { where?: unknown; result_shape?: string }
    return (
      body.result_shape !== 'directories' &&
      JSON.stringify(body.where) ===
        JSON.stringify({
          and: [{ tag: 'favorite' }, { tag: 'npc' }, { not: { tag: 'hidden' } }],
        })
    )
  })
  await page.getByRole('button', { name: 'Include NPC' }).click()
  await secondIncludeRequest

  const anyModeRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    if (url.pathname !== '/api/search/query' || request.method() !== 'POST') return false
    const body = request.postDataJSON() as { where?: unknown; result_shape?: string }
    return (
      body.result_shape !== 'directories' &&
      JSON.stringify(body.where) ===
        JSON.stringify({
          and: [{ or: [{ tag: 'favorite' }, { tag: 'npc' }] }, { not: { tag: 'hidden' } }],
        })
    )
  })
  await page.getByRole('button', { name: 'Any included tag' }).click()
  await anyModeRequest
  await expect(page.getByRole('button', { name: 'Any included tag' })).toHaveClass(/active/)
})

test('loads the full image after the thumbnail when a card is larger than its thumbnail', async ({ page }) => {
  let releaseFullImage!: () => void
  let fullImageRequested = false
  const fullImageCanFinish = new Promise<void>((resolve) => {
    releaseFullImage = resolve
  })
  const highResAsset = {
    ...sampleAsset,
    asset_id: 'asset-hires',
    relative_path: 'maps/hires.png',
    filename: 'hires.png',
    stem: 'hires',
    thumbnail_url: '/thumbs/hires-small.png',
    thumbnail_width: 12,
    thumbnail_height: 12,
  }

  await page.route('**/api/search?*', async (route) => {
    await route.fulfill({ json: { total: 1, cards: [{ kind: 'asset', asset: highResAsset }] } })
  })
  await page.route('**/api/search-with-directory-counts?*', async (route) => {
    await route.fulfill({ json: { directories: [sampleRootDirectory(), sampleDirectory()] } })
  })
  await page.route('**/thumbs/hires-small.png', async (route) => {
    await route.fulfill({
      contentType: 'image/gif',
      body: Buffer.from('R0lGODlhAQABAPAAAP///wAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==', 'base64'),
    })
  })
  await page.route('**/api/assets/asset-hires/file', async (route) => {
    fullImageRequested = true
    await fullImageCanFinish
    await route.fulfill({
      contentType: 'image/gif',
      body: Buffer.from('R0lGODlhAQABAPAAAP///wAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==', 'base64'),
    })
  })

  await page.goto('/')
  const image = page.getByRole('button', { name: /hires\.png/i }).locator('img')
  await expect(image).toHaveAttribute('src', '/thumbs/hires-small.png')
  await expect.poll(() => fullImageRequested).toBe(true)
  await expect(image).toHaveAttribute('src', '/thumbs/hires-small.png')

  releaseFullImage()

  await expect(image).toHaveAttribute('src', '/api/assets/asset-hires/file')
})

test('loads a larger thumbnail variant before the original file', async ({ page }) => {
  let smallVariantRequested = false
  let largeVariantRequested = false
  let originalRequested = false
  const mipmapAsset = {
    ...sampleAsset,
    asset_id: 'asset-mipmap',
    relative_path: 'maps/mipmap.png',
    filename: 'mipmap.png',
    stem: 'mipmap',
    thumbnail_url: '/thumbs/mipmap-12.png',
    thumbnail_width: 12,
    thumbnail_height: 12,
    thumbnail_variants: [
      { max_edge: 12, width: 12, height: 12, url: '/thumbs/mipmap-12.png' },
      { max_edge: 120, width: 120, height: 120, url: '/thumbs/mipmap-120.png' },
    ],
  }

  await page.route('**/api/search?*', async (route) => {
    await route.fulfill({ json: { total: 1, cards: [{ kind: 'asset', asset: mipmapAsset }] } })
  })
  await page.route('**/api/search-with-directory-counts?*', async (route) => {
    await route.fulfill({ json: { directories: [sampleRootDirectory(), sampleDirectory()] } })
  })
  await page.route('**/thumbs/mipmap-12.png', async (route) => {
    smallVariantRequested = true
    await route.fulfill({
      contentType: 'image/gif',
      body: Buffer.from('R0lGODlhAQABAPAAAP///wAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==', 'base64'),
    })
  })
  await page.route('**/thumbs/mipmap-120.png', async (route) => {
    largeVariantRequested = true
    await route.fulfill({
      contentType: 'image/gif',
      body: Buffer.from('R0lGODlhAQABAPAAAP///wAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==', 'base64'),
    })
  })
  await page.route('**/api/assets/asset-mipmap/file', async (route) => {
    originalRequested = true
    await route.fulfill({
      contentType: 'image/gif',
      body: Buffer.from('R0lGODlhAQABAPAAAP///wAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==', 'base64'),
    })
  })

  await page.goto('/')
  const image = page.getByRole('button', { name: /mipmap\.png/i }).locator('img')
  await expect.poll(() => smallVariantRequested).toBe(true)
  await expect.poll(() => largeVariantRequested).toBe(true)
  await expect(image).toHaveAttribute('src', '/thumbs/mipmap-120.png')
  expect(originalRequested).toBe(false)
})

test('shows grid thumbnails without cropping portrait images', async ({ page }) => {
  const portraitAsset = {
    ...sampleAsset,
    asset_id: 'asset-portrait-thumb',
    relative_path: 'maps/portrait.png',
    filename: 'portrait.png',
    stem: 'portrait',
    thumbnail_url: '/thumbs/portrait-256.png',
    thumbnail_width: 128,
    thumbnail_height: 256,
  }

  await page.route('**/api/search?*', async (route) => {
    await route.fulfill({ json: { total: 1, cards: [{ kind: 'asset', asset: portraitAsset }] } })
  })
  await page.route('**/api/search-with-directory-counts?*', async (route) => {
    await route.fulfill({ json: { directories: [sampleRootDirectory(), sampleDirectory()] } })
  })
  await page.route('**/thumbs/portrait-256.png', async (route) => {
    await route.fulfill({
      contentType: 'image/svg+xml',
      body: svgImage(128, 256),
    })
  })

  await page.goto('/')

  const image = page.getByRole('button', { name: /portrait\.png/i }).locator('.tile-thumb img')
  await expect(image).toBeVisible()
  await expect(image).toHaveCSS('object-fit', 'contain')

  const thumbBox = await page.getByRole('button', { name: /portrait\.png/i }).locator('.tile-thumb').boundingBox()
  const imageBox = await image.boundingBox()
  if (!thumbBox || !imageBox) throw new Error('portrait thumbnail layout is missing')

  expect(imageBox.height).toBeLessThanOrEqual(thumbBox.height)
  expect(imageBox.width).toBeLessThan(thumbBox.width * 0.6)
  expect(imageBox.height).toBeGreaterThan(thumbBox.height - 2)
})

test('sizes the selected landscape preview to the full panel width', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 })
  const wideAsset = {
    ...sampleAsset,
    asset_id: 'asset-wide-preview',
    relative_path: 'maps/wide-preview.png',
    filename: 'wide-preview.png',
    stem: 'wide-preview',
    thumbnail_url: '/thumbs/wide-preview-256.png',
    thumbnail_width: 256,
    thumbnail_height: 85,
  }

  await page.route('**/api/search?*', async (route) => {
    await route.fulfill({ json: { total: 1, cards: [{ kind: 'asset', asset: wideAsset }] } })
  })
  await page.route('**/api/search-with-directory-counts?*', async (route) => {
    await route.fulfill({ json: { directories: [sampleRootDirectory(), sampleDirectory()] } })
  })
  await page.route('**/api/assets/asset-wide-preview', async (route) => {
    await route.fulfill({
      json: {
        asset: wideAsset,
        actions: actionsFor(wideAsset, '/tmp/sample-assets/maps/wide-preview.png'),
      },
    })
  })
  await page.route('**/thumbs/wide-preview-256.png', async (route) => {
    await route.fulfill({
      contentType: 'image/gif',
      body: Buffer.from('R0lGODlhAQABAPAAAP///wAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==', 'base64'),
    })
  })
  await page.route('**/api/assets/asset-wide-preview/file', async (route) => {
    await route.fulfill({
      contentType: 'image/svg+xml',
      body: svgImage(768, 256),
    })
  })

  await page.goto('/')
  await page.getByRole('button', { name: /wide-preview\.png/i }).click()

  const panel = page.getByLabel('Selected asset')
  const preview = panel.locator('.preview')
  const previewImage = preview.locator('img')
  await expect(previewImage).toBeVisible()
  await expect(previewImage).toHaveCSS('object-fit', 'contain')

  const previewBox = await preview.boundingBox()
  const previewImageBox = await previewImage.boundingBox()
  const panelBox = await panel.boundingBox()
  if (!previewBox || !previewImageBox || !panelBox) throw new Error('selected landscape preview layout is missing')

  expect(previewBox.width).toBeLessThanOrEqual(panelBox.width)
  expect(previewBox.width).toBeGreaterThan(panelBox.width - 2)
  expect(previewImageBox.width).toBeGreaterThan(previewBox.width - 6)
  expect(previewBox.height).toBeLessThan(130)
})

test('uses a 512 thumbnail variant for the hover preview when available', async ({ page }) => {
  let originalRequested = false
  const hoverAsset = {
    ...sampleAsset,
    asset_id: 'asset-hover-preview',
    relative_path: 'maps/hover-preview.png',
    filename: 'hover-preview.png',
    stem: 'hover-preview',
    thumbnail_url: '/thumbs/hover-preview-256.png',
    thumbnail_width: 256,
    thumbnail_height: 128,
    thumbnail_variants: [
      { max_edge: 256, width: 256, height: 128, url: '/thumbs/hover-preview-256.png' },
      { max_edge: 512, width: 512, height: 256, url: '/thumbs/hover-preview-512.png' },
      { max_edge: 1024, width: 1024, height: 512, url: '/thumbs/hover-preview-1024.png' },
    ],
  }

  await page.route('**/api/search?*', async (route) => {
    await route.fulfill({ json: { total: 1, cards: [{ kind: 'asset', asset: hoverAsset }] } })
  })
  await page.route('**/api/search-with-directory-counts?*', async (route) => {
    await route.fulfill({ json: { directories: [sampleRootDirectory(), sampleDirectory()] } })
  })
  await page.route('**/thumbs/hover-preview-256.png', async (route) => {
    await route.fulfill({
      contentType: 'image/gif',
      body: Buffer.from('R0lGODlhAQABAPAAAP///wAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==', 'base64'),
    })
  })
  await page.route('**/thumbs/hover-preview-512.png', async (route) => {
    await route.fulfill({
      contentType: 'image/gif',
      body: Buffer.from('R0lGODlhAQABAPAAAP///wAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==', 'base64'),
    })
  })
  await page.route('**/thumbs/hover-preview-1024.png', async (route) => {
    await route.fulfill({
      contentType: 'image/gif',
      body: Buffer.from('R0lGODlhAQABAPAAAP///wAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==', 'base64'),
    })
  })
  await page.route('**/api/assets/asset-hover-preview/file', async (route) => {
    originalRequested = true
    await route.fulfill({
      contentType: 'image/gif',
      body: Buffer.from('R0lGODlhAQABAPAAAP///wAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==', 'base64'),
    })
  })

  await page.goto('/')
  await page.getByRole('button', { name: /hover-preview\.png/i }).hover()

  const hoverPreview = page.getByLabel('Hovered asset preview')
  const image = hoverPreview.locator('img')
  await expect(image).toBeVisible()
  await expect(image).toHaveAttribute('src', '/thumbs/hover-preview-512.png')
  await expect(hoverPreview).toHaveCSS('width', '512px')
  expect(originalRequested).toBe(false)
})

test('shows asset metadata in the hover preview and selected panel', async ({ page }) => {
  const metadataAsset = {
    ...sampleAsset,
    asset_id: 'asset-metadata-map',
    relative_path: 'maps/ancient-ruins.png',
    filename: 'ancient-ruins.png',
    stem: 'ancient-ruins',
    size_bytes: 14_680_064,
    media_width: 4096,
    media_height: 3072,
    thumbnail_url: '/thumbs/metadata-map.png',
    thumbnail_width: 256,
    thumbnail_height: 192,
  }

  await page.route('**/api/search?*', async (route) => {
    await route.fulfill({ json: { total: 1, cards: [{ kind: 'asset', asset: metadataAsset }] } })
  })
  await page.route('**/api/search-with-directory-counts?*', async (route) => {
    await route.fulfill({ json: { directories: [sampleRootDirectory(), sampleDirectory()] } })
  })
  await page.route('**/api/assets/asset-metadata-map', async (route) => {
    await route.fulfill({
      json: {
        asset: metadataAsset,
        actions: actionsFor(metadataAsset, '/tmp/sample-assets/maps/ancient-ruins.png'),
      },
    })
  })
  await page.route('**/thumbs/metadata-map.png', async (route) => {
    await route.fulfill({
      contentType: 'image/gif',
      body: Buffer.from('R0lGODlhAQABAPAAAP///wAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==', 'base64'),
    })
  })
  await page.route('**/api/assets/asset-metadata-map/file', async (route) => {
    await route.fulfill({
      contentType: 'image/gif',
      body: Buffer.from('R0lGODlhAQABAAAAACw=', 'base64'),
    })
  })

  await page.goto('/')
  await page.getByRole('button', { name: /ancient-ruins\.png/i }).hover()

  const hoverPreview = page.getByLabel('Hovered asset preview')
  await expect(hoverPreview).toContainText('4096 x 3072')
  await expect(hoverPreview).not.toContainText('14 MB')

  await page.getByRole('button', { name: /ancient-ruins\.png/i }).click()
  const panel = page.getByLabel('Selected asset')
  await expect(panel).toContainText('4096 x 3072')
  await expect(panel).toContainText('Size')
  await expect(panel).not.toContainText('maps/ancient-ruins.png')
  await expect(panel).not.toContainText('Sample Assets')
  await expect(panel).not.toContainText('maps')
  await expect(panel).not.toContainText('File size')
  await expect(panel).not.toContainText('14 MB')
  await expect(panel).not.toContainText('PNG')
})

test('loads the full image after resizing a card larger than its thumbnail', async ({ page }) => {
  let fullImageRequested = false
  const resizeAsset = {
    ...sampleAsset,
    asset_id: 'asset-resize-hires',
    relative_path: 'maps/resize-hires.png',
    filename: 'resize-hires.png',
    stem: 'resize-hires',
    thumbnail_url: '/thumbs/resize-hires-small.png',
    thumbnail_width: 80,
    thumbnail_height: 80,
  }

  await page.route('**/api/search?*', async (route) => {
    await route.fulfill({ json: { total: 1, cards: [{ kind: 'asset', asset: resizeAsset }] } })
  })
  await page.route('**/api/search-with-directory-counts?*', async (route) => {
    await route.fulfill({ json: { directories: [sampleRootDirectory(), sampleDirectory()] } })
  })
  await page.route('**/thumbs/resize-hires-small.png', async (route) => {
    await route.fulfill({
      contentType: 'image/gif',
      body: Buffer.from('R0lGODlhAQABAPAAAP///wAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==', 'base64'),
    })
  })
  await page.route('**/api/assets/asset-resize-hires/file', async (route) => {
    fullImageRequested = true
    await route.fulfill({
      contentType: 'image/gif',
      body: Buffer.from('R0lGODlhAQABAPAAAP///wAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==', 'base64'),
    })
  })

  await page.goto('/')
  await expect(page.getByRole('button', { name: /resize-hires\.png/i })).toBeVisible()
  const image = page.getByRole('button', { name: /resize-hires\.png/i }).locator('img')
  await expect(image).toHaveAttribute('src', '/thumbs/resize-hires-small.png')
  await expect.poll(() => fullImageRequested).toBe(false)

  for (let index = 12; index > 5; index -= 1) {
    await page.getByRole('button', { name: 'Show fewer cards in grid' }).click()
  }

  await expect.poll(() => fullImageRequested).toBe(true)
  await expect(image).toHaveAttribute('src', '/api/assets/asset-resize-hires/file')
})

test('renders the initial card page before refreshing directory state', async ({ page }) => {
  const searchRequests: string[] = []
  let initialSearchCompleted = false
  let directoryRefreshStarted = false

  await page.unroute('**/api/search?*')
  await page.unroute('**/api/search-with-directory-counts?*')
  await page.route('**/api/search?*', async (route) => {
    const url = new URL(route.request().url())
    searchRequests.push(url.search)
    await route.fulfill({ json: { total: 1, cards: [{ kind: 'asset', asset: sampleAsset }] } })
    initialSearchCompleted = true
  })
  await page.route('**/api/search-with-directory-counts?*', async (route) => {
    directoryRefreshStarted = true
    expect(initialSearchCompleted).toBe(true)
    await route.fulfill({
      json: {
        directories: [sampleRootDirectory(), sampleDirectory()],
      },
    })
  })

  await page.goto('/')
  await expect(page.getByRole('button', { name: /castle\.png/i })).toBeVisible()
  await expect.poll(() => directoryRefreshStarted).toBe(true)

  const firstSearch = new URLSearchParams(searchRequests[0])
  expect(firstSearch.get('limit')).toBe('100')
  expect(firstSearch.get('offset')).toBe('0')
  expect(searchRequests.some((request) => new URLSearchParams(request).get('offset') === '30')).toBe(false)
})

test('uses the server next offset when loading more search results', async ({ page }) => {
  await disableAutomaticInfiniteScroll(page)
  const searchRequests: string[] = []
  const secondPageAsset = {
    ...sampleAsset,
    asset_id: 'asset-tower',
    relative_path: 'maps/tower.png',
    filename: 'tower.png',
    stem: 'tower',
  }

  await page.unroute('**/api/search?*')
  await page.route('**/api/search?*', async (route) => {
    const url = new URL(route.request().url())
    searchRequests.push(url.search)
    const offset = Number(url.searchParams.get('offset') ?? 0)
    await route.fulfill({
      json: {
        total: 150,
        next_offset: offset === 0 ? 100 : 150,
        cards: [{ kind: 'asset', asset: offset === 0 ? sampleAsset : secondPageAsset }],
      },
    })
  })

  await page.goto('/')
  const typedSearchRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return url.pathname === '/api/search' && url.searchParams.get('q') === 'castle' && url.searchParams.get('offset') === '0'
  })
  await page.getByRole('searchbox', { name: 'Search assets' }).fill('castle')
  await typedSearchRequest
  await expect(page.getByRole('button', { name: 'Load more' })).toBeVisible()

  const loadMoreRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return url.pathname === '/api/search' && url.searchParams.get('q') === 'castle' && url.searchParams.get('offset') === '100'
  })
  await page.getByRole('button', { name: 'Load more' }).click()
  await loadMoreRequest

  await expect
    .poll(() => searchRequests.map((request) => new URLSearchParams(request).get('offset')))
    .toContain('100')
  expect(searchRequests.map((request) => new URLSearchParams(request).get('offset'))).not.toContain('1')
})

test('keeps loading past grouped exact search results', async ({ page }) => {
  await disableAutomaticInfiniteScroll(page)
  const collection = {
    kind: 'collection',
    collection_key: 'goblin-variants',
    representative: sampleAsset,
    variants: [sampleAsset, { ...sampleAsset, asset_id: 'asset-goblin-2', filename: 'goblin2.png' }],
    count: 2,
  }
  const archer = {
    ...sampleAsset,
    asset_id: 'asset-goblin-archer',
    filename: 'goblin archer.png',
    stem: 'goblin archer',
    relative_path: 'maps/goblin archer.png',
  }
  const searchRequests: string[] = []

  await page.unroute('**/api/search?*')
  await page.route('**/api/search?*', async (route) => {
    const url = new URL(route.request().url())
    searchRequests.push(url.search)
    const offset = Number(url.searchParams.get('offset') ?? 0)
    await route.fulfill({
      json: {
        total: 3,
        next_offset: offset === 0 ? 1 : 2,
        has_more: offset === 0,
        cards: offset === 0 ? [collection] : [{ kind: 'asset', asset: archer }],
      },
    })
  })

  await page.goto('/')
  const typedSearchRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return url.pathname === '/api/search' && url.searchParams.get('q') === 'Goblin' && url.searchParams.get('offset') === '0'
  })
  await page.getByRole('searchbox', { name: 'Search assets' }).fill('Goblin')
  await typedSearchRequest
  await expect(page.getByRole('button', { name: /castle\.png/i })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Load more' })).toBeVisible()

  await page.getByRole('button', { name: 'Load more' }).click()

  await expect(page.getByRole('button', { name: /goblin archer\.png/i })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Loading' })).toHaveCount(0)
  expect(searchRequests.map((request) => new URLSearchParams(request).get('offset'))).toContain('1')
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

test('uses folder rows as transient view scopes', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('button', { name: /castle\.png/i })).toBeVisible()
  await revealSampleFolder(page)

  const viewRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return (
      url.pathname === '/api/search' &&
      url.searchParams.get('view_source_id') === 'sample' &&
      url.searchParams.get('view_directory') === 'maps'
    )
  })
  await page.getByRole('group', { name: 'maps folder' }).getByRole('button', { name: 'maps', exact: true }).click()
  await viewRequest
  await expect(page.getByRole('group', { name: 'maps folder' })).toHaveClass(/view-folder/)

  const scopedSearchRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return (
      url.pathname === '/api/search' &&
      url.searchParams.get('q') === 'castle' &&
      url.searchParams.get('view_source_id') === 'sample' &&
      url.searchParams.get('view_directory') === 'maps'
    )
  })
  await page.getByRole('searchbox', { name: 'Search assets' }).fill('castle')
  await scopedSearchRequest
})

test('updates folder view tree before scoped responses arrive', async ({ page }) => {
  let releaseScopedSearch!: () => void
  let releaseScopedDirectoryState!: () => void
  const scopedSearchCanFinish = new Promise<void>((resolve) => {
    releaseScopedSearch = resolve
  })
  const scopedDirectoryStateCanFinish = new Promise<void>((resolve) => {
    releaseScopedDirectoryState = resolve
  })
  const tokenDirectory = {
    id: 2,
    source_id: 'sample',
    path: 'tokens',
    name: 'tokens',
    depth: 1,
    asset_count: 1,
    matched_asset_count: 1,
    included_asset_count: 1,
    quality: 5,
    quality_override: null,
    selection_state: null,
    selection_effective: true,
    profile_dirty: false,
    has_descendant_whitelist: false,
    has_descendant_blacklist: false,
  }
  const nestedMapDirectory = {
    id: 3,
    source_id: 'sample',
    path: 'maps/sub',
    name: 'sub',
    depth: 2,
    asset_count: 1,
    matched_asset_count: 1,
    included_asset_count: 1,
    quality: 5,
    quality_override: null,
    selection_state: null,
    selection_effective: true,
    profile_dirty: false,
    has_descendant_whitelist: false,
    has_descendant_blacklist: false,
  }

  await page.unroute('**/api/search?*')
  await page.unroute('**/api/search-with-directory-counts?*')
  await page.route('**/api/search?*', async (route) => {
    const url = new URL(route.request().url())
    if (url.searchParams.get('view_directory') === 'maps/sub') {
      await scopedSearchCanFinish
    }
    await route.fulfill({ json: { total: 1, cards: [{ kind: 'asset', asset: sampleAsset }] } })
  })
  await page.route('**/api/search-with-directory-counts?*', async (route) => {
    const url = new URL(route.request().url())
    const directories = [sampleRootDirectory(), sampleDirectory(), nestedMapDirectory, tokenDirectory]
    if (url.searchParams.get('view_directory') === 'maps/sub') {
      await scopedDirectoryStateCanFinish
    }
    await route.fulfill({
      json: {
        ...(url.searchParams.get('directory_state_only') === 'true'
          ? { directory_states: directories.map(directoryState) }
          : { directories }),
      },
    })
  })

  await page.goto('/')
  await expect(page.getByRole('button', { name: /castle\.png/i })).toBeVisible()
  await revealSampleFolder(page)
  await expect(page.getByRole('group', { name: 'sub folder' })).toBeVisible()
  await expect(page.getByRole('group', { name: 'tokens folder' })).toBeVisible()

  const viewRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return (
      url.pathname === '/api/search' &&
      url.searchParams.get('view_source_id') === 'sample' &&
      url.searchParams.get('view_directory') === 'maps/sub'
    )
  })
  await page.getByRole('group', { name: 'sub folder' }).getByRole('button', { name: 'sub', exact: true }).click()
  await viewRequest

  await expect(page.getByRole('group', { name: 'Sample Assets folder' })).toBeVisible()
  await expect(page.getByRole('group', { name: 'maps folder' })).toBeVisible()
  await expect(page.getByRole('group', { name: 'sub folder' })).toHaveClass(/view-folder/)
  await expect(page.getByRole('group', { name: 'tokens folder' })).toHaveCount(0)

  releaseScopedSearch()
  releaseScopedDirectoryState()
})

test('sends sidebar whitelist and blacklist rules with folder shortcuts', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('button', { name: /castle\.png/i })).toBeVisible()
  await revealSampleFolder(page)

  const whitelistSettingRequest = waitForProfileSetting(page, {
    source_id: 'sample',
    directory: 'maps',
    selection_state: 'whitelist',
  })
  await page.getByRole('group', { name: 'maps folder' }).getByRole('button', { name: 'maps', exact: true }).click({
    modifiers: ['Shift'],
  })

  expectProfileSettingRequest(await whitelistSettingRequest, {
    source_id: 'sample',
    directory: 'maps',
    selection_state: 'whitelist',
  })

  const blacklistSettingRequest = waitForProfileSetting(page, {
    source_id: 'sample',
    directory: 'maps',
    selection_state: 'blacklist',
  })
  await page.getByRole('group', { name: 'maps folder' }).getByRole('button', { name: 'maps', exact: true }).click({
    modifiers: ['Alt'],
  })

  expectProfileSettingRequest(await blacklistSettingRequest, {
    source_id: 'sample',
    directory: 'maps',
    selection_state: 'blacklist',
  })
  await expect(page.getByRole('group', { name: 'maps folder' })).toBeVisible()
})

test('updates folder selection state before refreshed results arrive', async ({ page }) => {
  let releasePatch!: () => void
  const patchCanFinish = new Promise<void>((resolve) => {
    releasePatch = resolve
  })
  await page.route('**/api/search-profile/settings', async (route) => {
    await patchCanFinish
    await route.fulfill({
      json: { session: profileSession(false, true) },
    })
  })

  await page.goto('/')
  await expect(page.getByRole('button', { name: /castle\.png/i })).toBeVisible()
  await revealSampleFolder(page)

  const whitelistSettingRequest = waitForProfileSetting(page, {
    source_id: 'sample',
    directory: 'maps',
    selection_state: 'whitelist',
  })
  await page.getByRole('group', { name: 'maps folder' }).getByRole('button', { name: 'maps', exact: true }).click({
    modifiers: ['Shift'],
  })
  await whitelistSettingRequest

  await expect(page.getByRole('button', { name: 'Folder controls for maps, focused, quality 4' })).toBeVisible()
  await expect(page.getByRole('button', { name: /castle\.png/i })).toBeVisible()

  const patchResponse = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return url.pathname === '/api/search-profile/settings'
  })
  releasePatch()
  await patchResponse
})

test('combines folder filtering and quality in one control popover', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('button', { name: /castle\.png/i })).toBeVisible()
  await revealSampleFolder(page)

  await page.getByRole('button', { name: 'Folder controls for maps, quality 4' }).click()
  await expect(page.getByRole('group', { name: 'Folder controls for maps', exact: true })).toBeVisible()

  const focusedSettingRequest = waitForProfileSetting(page, {
    source_id: 'sample',
    directory: 'maps',
    selection_state: 'whitelist',
  })
  await page.getByRole('button', { name: 'Focus maps' }).click()

  expectProfileSettingRequest(await focusedSettingRequest, {
    source_id: 'sample',
    directory: 'maps',
    selection_state: 'whitelist',
  })
})

test('shows superseded whitelist state in the folder control', async ({ page }) => {
  await page.route('**/api/search-with-directory-counts?*', async (route) => {
    await route.fulfill({
      json: {
        total: 1,
        cards: [{ kind: 'asset', asset: sampleAsset }],
        directories: [
          sampleRootDirectory(),
          sampleDirectory(4, null, 'whitelist', false),
          {
            source_id: 'sample',
            path: 'maps/nested',
            name: 'nested',
            depth: 2,
            asset_count: 1,
            matched_asset_count: 1,
            included_asset_count: 1,
            quality: 5,
            quality_override: null,
            selection_state: 'whitelist',
            selection_effective: true,
          },
        ],
      },
    })
  })

  await page.goto('/')
  await expect(page.getByRole('button', { name: /castle\.png/i })).toBeVisible()
  await revealSampleFolder(page)

  await expect(page.getByRole('button', { name: 'Folder controls for maps, superseded focus, quality 4' })).toBeVisible()
  await expect(page.getByRole('group', { name: 'maps folder' })).toHaveClass(/whitelist-superseded/)
})

test('marks folders that contain unsaved profile changes', async ({ page }) => {
  await page.route('**/api/search-with-directory-counts?*', async (route) => {
    await route.fulfill({
      json: {
        total: 1,
        cards: [{ kind: 'asset', asset: sampleAsset }],
        directories: [
          { ...sampleRootDirectory(), profile_dirty: true },
          sampleDirectory(4, null, null, true, true),
          {
            source_id: 'sample',
            path: 'maps/sub',
            name: 'sub',
            depth: 2,
            asset_count: 1,
            matched_asset_count: 1,
            included_asset_count: 1,
            quality: 8,
            quality_override: 8,
            selection_state: null,
            selection_effective: true,
            profile_dirty: true,
          },
          {
            source_id: 'sample',
            path: 'maps/saved',
            name: 'saved',
            depth: 2,
            asset_count: 1,
            matched_asset_count: 1,
            included_asset_count: 1,
            quality: 7,
            quality_override: 7,
            selection_state: null,
            selection_effective: true,
            profile_dirty: false,
          },
        ],
      },
    })
  })

  await page.goto('/')
  await expect(page.getByRole('button', { name: /castle\.png/i })).toBeVisible()

  await expect(
    page.getByRole('group', { name: 'Sample Assets folder' }).getByRole('button', { name: /quality 5/ })
  ).toHaveAttribute('title', 'Unsaved profile change in this folder subtree')

  await revealSampleFolder(page)

  await expect(
    page.getByRole('group', { name: 'maps folder' }).getByRole('button', { name: /quality 4/ })
  ).toHaveAttribute('title', 'Unsaved profile change in this folder subtree')
  await expect(
    page.getByRole('group', { name: 'sub folder' }).getByRole('button', { name: /quality 8/ })
  ).toHaveAttribute('title', 'Unsaved profile change in this folder subtree')
  await expect(
    page.getByRole('group', { name: 'saved folder' }).getByRole('button', { name: /quality 7/ })
  ).not.toHaveAttribute('title', 'Unsaved profile change in this folder subtree')
})

test('summarizes dirty and descendant selection signals in the folder control', async ({ page }) => {
  await page.route('**/api/search-with-directory-counts?*', async (route) => {
    await route.fulfill({
      json: {
        total: 1,
        cards: [{ kind: 'asset', asset: sampleAsset }],
        directories: [
          {
            ...sampleRootDirectory(),
            profile_dirty: true,
            has_descendant_whitelist: true,
            has_descendant_blacklist: true,
          },
          sampleDirectory(4, null, null, true, true, true, true),
          {
            source_id: 'sample',
            path: 'maps/focused',
            name: 'focused',
            depth: 2,
            asset_count: 1,
            matched_asset_count: 1,
            included_asset_count: 1,
            quality: 5,
            quality_override: null,
            selection_state: 'whitelist',
            selection_effective: true,
            profile_dirty: false,
            has_descendant_whitelist: false,
            has_descendant_blacklist: false,
          },
          {
            source_id: 'sample',
            path: 'maps/blocked',
            name: 'blocked',
            depth: 2,
            asset_count: 1,
            matched_asset_count: 1,
            included_asset_count: 1,
            quality: 5,
            quality_override: null,
            selection_state: 'blacklist',
            selection_effective: true,
            profile_dirty: false,
            has_descendant_whitelist: false,
            has_descendant_blacklist: false,
          },
        ],
      },
    })
  })

  await page.goto('/')
  await expect(page.getByRole('button', { name: /castle\.png/i })).toBeVisible()

  await expect(
    page.getByRole('group', { name: 'Sample Assets folder' }).getByRole('button', { name: /quality 5/ })
  ).toHaveAttribute('title', 'Unsaved profile change; descendant focus rule; descendant block rule')
  await expect(
    page.getByRole('group', { name: 'Sample Assets folder' }).locator('.folder-subtree-signals .dirty-signal')
  ).toBeVisible()
  await expect(
    page.getByRole('group', { name: 'Sample Assets folder' }).locator('.folder-subtree-signals .whitelist-signal')
  ).toBeVisible()
  await expect(
    page.getByRole('group', { name: 'Sample Assets folder' }).locator('.folder-subtree-signals .blacklist-signal')
  ).toBeVisible()
  const sourceControl = page
    .getByRole('group', { name: 'Sample Assets folder' })
    .getByRole('button', { name: /quality 5/ })
  const dirtySignal = page.getByRole('group', { name: 'Sample Assets folder' }).locator('.dirty-signal')
  const whitelistSignal = page.getByRole('group', { name: 'Sample Assets folder' }).locator('.whitelist-signal')
  const blacklistSignal = page.getByRole('group', { name: 'Sample Assets folder' }).locator('.blacklist-signal')
  const controlBox = await sourceControl.boundingBox()
  const dirtyBox = await dirtySignal.boundingBox()
  const whitelistBox = await whitelistSignal.boundingBox()
  const blacklistBox = await blacklistSignal.boundingBox()
  if (!controlBox || !dirtyBox || !whitelistBox || !blacklistBox) throw new Error('folder signal geometry is missing')

  expect(dirtyBox.height).toBeGreaterThanOrEqual(3)
  expect(dirtyBox.height).toBeLessThanOrEqual(5)
  expect(whitelistBox.height).toBeGreaterThanOrEqual(3)
  expect(whitelistBox.height).toBeLessThanOrEqual(5)
  expect(blacklistBox.height).toBeGreaterThanOrEqual(3)
  expect(blacklistBox.height).toBeLessThanOrEqual(5)
  expect(Math.abs(dirtyBox.y - whitelistBox.y)).toBeLessThanOrEqual(1)
  expect(Math.abs(dirtyBox.y - blacklistBox.y)).toBeLessThanOrEqual(1)
  expect(Math.abs(dirtyBox.x - controlBox.x)).toBeLessThanOrEqual(1)
  expect(Math.abs(dirtyBox.x + dirtyBox.width - whitelistBox.x)).toBeLessThanOrEqual(1)
  expect(Math.abs(whitelistBox.x + whitelistBox.width - blacklistBox.x)).toBeLessThanOrEqual(1)
  expect(Math.abs(blacklistBox.x + blacklistBox.width - (controlBox.x + controlBox.width))).toBeLessThanOrEqual(1)
  expect(controlBox.y + controlBox.height - (dirtyBox.y + dirtyBox.height)).toBeLessThanOrEqual(1)

  await revealSampleFolder(page)

  await expect(
    page.getByRole('group', { name: 'maps folder' }).getByRole('button', { name: /quality 4/ })
  ).toHaveAttribute('title', 'Unsaved profile change; descendant focus rule; descendant block rule')
})

test('folder quality button shortcuts toggle folder rules without opening the menu', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('button', { name: /castle\.png/i })).toBeVisible()
  await revealSampleFolder(page)

  const focusedSettingRequest = waitForProfileSetting(page, {
    source_id: 'sample',
    directory: 'maps',
    selection_state: 'whitelist',
  })
  await page.getByRole('button', { name: 'Folder controls for maps, quality 4' }).click({ modifiers: ['Shift'] })

  expectProfileSettingRequest(await focusedSettingRequest, {
    source_id: 'sample',
    directory: 'maps',
    selection_state: 'whitelist',
  })
  await expect(page.getByRole('group', { name: 'Folder controls for maps', exact: true })).toHaveCount(0)

  const blockedSettingRequest = waitForProfileSetting(page, {
    source_id: 'sample',
    directory: 'maps',
    selection_state: 'blacklist',
  })
  await page.getByRole('button', { name: 'Folder controls for maps, focused, quality 4' }).click({
    modifiers: ['Alt'],
  })

  expectProfileSettingRequest(await blockedSettingRequest, {
    source_id: 'sample',
    directory: 'maps',
    selection_state: 'blacklist',
  })
  await expect(page.getByRole('group', { name: 'Folder controls for maps', exact: true })).toHaveCount(0)
})

test('keeps sidebar rules across search text and lane changes', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('button', { name: /castle\.png/i })).toBeVisible()
  await revealSampleFolder(page)
  const whitelistSettingRequest = waitForProfileSetting(page, {
    source_id: 'sample',
    directory: 'maps',
    selection_state: 'whitelist',
  })
  await page.getByRole('group', { name: 'maps folder' }).getByRole('button', { name: 'maps', exact: true }).click({
    modifiers: ['Shift'],
  })
  const whitelistSetting = await whitelistSettingRequest
  const searchSession = (whitelistSetting.postDataJSON() as { search_session: string }).search_session

  const filteredSearchRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return (
      url.pathname === '/api/search' &&
      url.searchParams.get('q') === 'castle' &&
      url.searchParams.get('search_session') === searchSession
    )
  })
  await page.getByRole('searchbox', { name: 'Search assets' }).fill('castle')
  expectSearchSession(await filteredSearchRequest, searchSession)

  const domainSearchRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return (
      url.pathname === '/api/search' &&
      url.searchParams.get('q') === 'castle' &&
      url.searchParams.get('mode') === 'domain' &&
      url.searchParams.get('search_session') === searchSession
    )
  })
  await page.getByRole('tab', { name: 'Domain' }).click()

  expectSearchSession(await domainSearchRequest, searchSession)
})

test('sends independent sibling whitelist rules through the profile API', async ({ page }) => {
  await page.route('**/api/search-with-directory-counts?*', async (route) => {
    await route.fulfill({
      json: {
        total: 1,
        cards: [{ kind: 'asset', asset: sampleAsset }],
        directories: [
          sampleRootDirectory(),
          sampleDirectory(),
          {
            source_id: 'sample',
            path: 'maps/sub',
            name: 'sub',
            depth: 2,
            asset_count: 1,
            matched_asset_count: 1,
            included_asset_count: 1,
            quality: 5,
            quality_override: null,
            selection_state: null,
          },
          {
            source_id: 'sample',
            path: 'tokens',
            name: 'tokens',
            depth: 1,
            asset_count: 1,
            matched_asset_count: 1,
            included_asset_count: 1,
            quality: 5,
            quality_override: null,
            selection_state: null,
          },
        ],
      },
    })
  })

  await page.goto('/')
  await expect(page.getByRole('button', { name: /castle\.png/i })).toBeVisible()
  await revealSampleFolder(page)

  const parentWhitelistRequest = waitForProfileSetting(page, {
    source_id: 'sample',
    directory: 'maps',
    selection_state: 'whitelist',
  })
  await page.getByRole('group', { name: 'maps folder' }).getByRole('button', { name: 'maps', exact: true }).click({
    modifiers: ['Shift'],
  })
  expectProfileSettingRequest(await parentWhitelistRequest, {
    source_id: 'sample',
    directory: 'maps',
    selection_state: 'whitelist',
  })

  const siblingWhitelistRequest = waitForProfileSetting(page, {
    source_id: 'sample',
    directory: 'tokens',
    selection_state: 'whitelist',
  })
  await page.getByRole('group', { name: 'tokens folder' }).getByRole('button', { name: 'tokens', exact: true }).click({
    modifiers: ['Shift'],
  })

  expectProfileSettingRequest(await siblingWhitelistRequest, {
    source_id: 'sample',
    directory: 'tokens',
    selection_state: 'whitelist',
  })
})

test('highlights the hovered asset folder path', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('button', { name: /castle\.png/i })).toBeVisible()
  await revealSampleFolder(page)

  await page.getByRole('button', { name: /castle\.png/i }).hover()

  await expect(page.getByRole('group', { name: 'Sample Assets folder' })).toHaveClass(/path-highlight/)
  await expect(page.getByRole('group', { name: 'maps folder' })).toHaveClass(/direct-highlight/)
})

test('selects an audio card when clicking beneath the hidden preview volume control', async ({ page }) => {
  const audioAsset = {
    ...sampleAsset,
    asset_id: 'asset-thunder',
    relative_path: 'sounds/thunder.mp3',
    filename: 'thunder.mp3',
    stem: 'thunder',
    extension: '.mp3',
    directory: 'sounds',
    media_type: 'audio',
    duration_seconds: 125,
  }

  await page.addInitScript(() => {
    Object.defineProperty(HTMLMediaElement.prototype, 'play', {
      configurable: true,
      value() {
        return Promise.resolve()
      },
    })
  })

  await page.route('**/api/search-with-directory-counts?*', async (route) => {
    await route.fulfill({
      json: {
        total: 1,
        cards: [{ kind: 'asset', asset: audioAsset }],
        directories: [
          sampleRootDirectory(),
          {
            source_id: 'sample',
            path: 'sounds',
            name: 'sounds',
            depth: 1,
            asset_count: 1,
            matched_asset_count: 1,
            included_asset_count: 1,
            quality: 5,
            quality_override: null,
            selection_state: null,
          },
        ],
      },
    })
  })
  await page.route('**/api/search?*', async (route) => {
    await route.fulfill({
      json: {
        total: 1,
        cards: [{ kind: 'asset', asset: audioAsset }],
      },
    })
  })
  await page.route('**/api/assets/asset-thunder', async (route) => {
    await route.fulfill({
      json: {
        asset: audioAsset,
        actions: actionsFor(audioAsset, '/tmp/sample-assets/sounds/thunder.mp3'),
      },
    })
  })
  await page.route('**/api/assets/asset-thunder/file', async (route) => {
    await route.fulfill({
      contentType: 'audio/mpeg',
      body: Buffer.from('audio'),
    })
  })

  await page.goto('/')
  const tile = page.getByRole('button', { name: /thunder\.mp3/i })
  await tile.hover({ position: { x: 8, y: 8 } })
  const audioCard = page.getByRole('group', { name: 'Audio asset card for thunder.mp3' })
  const muteButton = audioCard.getByRole('button', { name: 'Unmute preview' })
  const muteButtonBox = await muteButton.boundingBox()
  if (!muteButtonBox) throw new Error('mute button is missing')
  await page.mouse.move(muteButtonBox.x + muteButtonBox.width / 2, muteButtonBox.y + muteButtonBox.height + 32)
  await expect(audioCard.getByRole('slider', { name: 'Preview volume' })).toBeHidden()

  await page.mouse.click(muteButtonBox.x + muteButtonBox.width / 2, muteButtonBox.y + muteButtonBox.height + 32)

  await expect(page.getByLabel('Selected asset')).toContainText('thunder.mp3')
})

test('previews audio on hover and shows playback controls when selected', async ({ page }) => {
  const audioAsset = {
    ...sampleAsset,
    asset_id: 'asset-thunder',
    relative_path: 'sounds/thunder.mp3',
    filename: 'thunder.mp3',
    stem: 'thunder',
    extension: '.mp3',
    directory: 'sounds',
    media_type: 'audio',
    duration_seconds: 125,
  }
  const secondAudioAsset = {
    ...audioAsset,
    asset_id: 'asset-rain',
    relative_path: 'sounds/rain.mp3',
    filename: 'rain.mp3',
    stem: 'rain',
  }

  await page.addInitScript(() => {
    Object.defineProperty(window, '__audioEvents', {
      configurable: true,
      writable: true,
      value: [],
    })
    Object.defineProperty(HTMLMediaElement.prototype, 'play', {
      configurable: true,
      value(this: HTMLMediaElement) {
        ;(window as unknown as { __audioEvents: string[] }).__audioEvents.push(
          `play:${this.getAttribute('src') ?? ''}:muted=${this.muted}:volume=${this.volume}`
        )
        return Promise.resolve()
      },
    })
    Object.defineProperty(HTMLMediaElement.prototype, 'pause', {
      configurable: true,
      value(this: HTMLMediaElement) {
        ;(window as unknown as { __audioEvents: string[] }).__audioEvents.push(
          `pause:${this.getAttribute('src') ?? ''}`
        )
      },
    })
  })

  await page.route('**/api/search-with-directory-counts?*', async (route) => {
    await route.fulfill({
      json: {
        total: 2,
        cards: [
          { kind: 'asset', asset: audioAsset },
          { kind: 'asset', asset: secondAudioAsset },
        ],
        directories: [
          sampleRootDirectory(),
          {
            source_id: 'sample',
            path: 'sounds',
            name: 'sounds',
            depth: 1,
            asset_count: 1,
            matched_asset_count: 1,
            included_asset_count: 1,
            quality: 5,
            quality_override: null,
            selection_state: null,
          },
        ],
      },
    })
  })
  await page.route('**/api/search?*', async (route) => {
    await route.fulfill({
      json: {
        total: 2,
        cards: [
          { kind: 'asset', asset: audioAsset },
          { kind: 'asset', asset: secondAudioAsset },
        ],
      },
    })
  })
  await page.route('**/api/assets/asset-thunder', async (route) => {
    await route.fulfill({
      json: {
        asset: audioAsset,
        actions: actionsFor(audioAsset, '/tmp/sample-assets/sounds/thunder.mp3'),
      },
    })
  })
  await page.route('**/api/assets/asset-thunder/file', async (route) => {
    await route.fulfill({
      contentType: 'audio/mpeg',
      body: Buffer.from('audio'),
    })
  })
  await page.route('**/api/assets/asset-rain/file', async (route) => {
    await route.fulfill({
      contentType: 'audio/mpeg',
      body: Buffer.from('audio'),
    })
  })

  await page.goto('/')
  const tile = page.getByRole('button', { name: /thunder\.mp3/i })
  await expect(tile).toBeVisible()

  await tile.hover({ position: { x: 8, y: 8 } })
  await expect.poll(() => page.evaluate(() => (window as unknown as { __audioEvents: string[] }).__audioEvents)).toContain(
    'play:/api/assets/asset-thunder/file:muted=true:volume=0.5'
  )
  const audioCard = page.getByRole('group', { name: 'Audio asset card for thunder.mp3' })
  await expect(audioCard.getByRole('button', { name: 'Unmute preview' })).toBeVisible()
  await expect(audioCard.getByRole('slider', { name: 'Preview volume' })).toBeHidden()
  await expect(page.getByLabel('Hovered asset preview').getByRole('button', { name: 'Unmute preview' })).toHaveCount(0)
  await expect(page.getByLabel('Hovered asset preview')).toContainText('2:05')

  const muteButton = audioCard.getByRole('button', { name: 'Unmute preview' })
  const muteButtonBox = await muteButton.boundingBox()
  if (!muteButtonBox) throw new Error('mute button is missing')
  await page.mouse.move(muteButtonBox.x + muteButtonBox.width / 2, muteButtonBox.y + muteButtonBox.height + 32)
  await expect(audioCard.getByRole('slider', { name: 'Preview volume' })).toBeHidden()

  await muteButton.hover()
  await expect(audioCard.getByRole('slider', { name: 'Preview volume' })).toBeVisible()
  await expect(audioCard.getByRole('slider', { name: 'Preview volume' })).toHaveValue('50')
  const sliderBox = await audioCard.getByRole('slider', { name: 'Preview volume' }).boundingBox()
  if (!sliderBox) throw new Error('preview slider is missing')
  expect(sliderBox.x).toBeGreaterThanOrEqual(muteButtonBox.x - 44)
  expect(sliderBox.x + sliderBox.width).toBeLessThanOrEqual(muteButtonBox.x + muteButtonBox.width + 44)

  await audioCard.getByRole('button', { name: 'Unmute preview' }).click()
  await expect(audioCard.getByRole('button', { name: 'Mute preview' })).toBeVisible()
  await expect
    .poll(() => page.locator('audio').first().evaluate((audio) => (audio as HTMLAudioElement).muted))
    .toBe(false)

  await audioCard.getByRole('slider', { name: 'Preview volume' }).fill('70')
  await expect
    .poll(() => page.locator('audio').first().evaluate((audio) => (audio as HTMLAudioElement).volume))
    .toBe(0.7)

  const secondCard = page.getByRole('group', { name: 'Audio asset card for rain.mp3' })
  await secondCard.hover()
  await expect(audioCard.getByRole('button', { name: 'Mute preview' })).toBeHidden()
  await expect(secondCard.getByRole('button', { name: 'Mute preview' })).toBeVisible()

  await page.mouse.move(0, 0)
  await expect.poll(() => page.evaluate(() => (window as unknown as { __audioEvents: string[] }).__audioEvents)).toContain(
    'pause:/api/assets/asset-thunder/file'
  )

  await page.evaluate(() => {
    ;(window as unknown as { __audioEvents: string[] }).__audioEvents = []
  })
  await tile.click({ position: { x: 8, y: 8 } })
  await expect.poll(() => page.evaluate(() => (window as unknown as { __audioEvents: string[] }).__audioEvents)).toContain(
    'pause:/api/assets/asset-thunder/file'
  )
  const selectedPanel = page.getByLabel('Selected asset')
  await expect(selectedPanel.locator('audio[controls]')).toHaveAttribute('src', '/api/assets/asset-thunder/file')
  await expect(selectedPanel).toContainText('Length')
  await expect(selectedPanel).toContainText('2:05')
})

test('selecting an asset unfolds and scrolls to its folder path without refolding user-opened folders', async ({ page }) => {
  const farAsset = {
    ...sampleAsset,
    asset_id: 'asset-obelisk',
    relative_path: 'folder-32/deep/obelisk.png',
    filename: 'obelisk.png',
    stem: 'obelisk',
    directory: 'folder-32/deep',
  }
  const directories = [
    sampleRootDirectory(),
    {
      source_id: 'sample',
      path: 'tokens',
      name: 'tokens',
      depth: 1,
      asset_count: 1,
      matched_asset_count: 1,
      included_asset_count: 1,
      quality: 5,
      quality_override: null,
      selection_state: null,
    },
    {
      source_id: 'sample',
      path: 'tokens/hidden',
      name: 'hidden',
      depth: 2,
      asset_count: 1,
      matched_asset_count: 1,
      included_asset_count: 1,
      quality: 5,
      quality_override: null,
      selection_state: null,
    },
    ...Array.from({ length: 40 }, (_, index) => ({
      source_id: 'sample',
      path: `folder-${String(index).padStart(2, '0')}`,
      name: `folder-${String(index).padStart(2, '0')}`,
      depth: 1,
      asset_count: 1,
      matched_asset_count: 1,
      included_asset_count: 1,
      quality: 5,
      quality_override: null,
      selection_state: null,
    })),
    {
      source_id: 'sample',
      path: 'folder-32/deep',
      name: 'deep',
      depth: 2,
      asset_count: 1,
      matched_asset_count: 1,
      included_asset_count: 1,
      quality: 5,
      quality_override: null,
      selection_state: null,
    },
  ]

  await page.route('**/api/search-with-directory-counts?*', async (route) => {
    await route.fulfill({ json: { total: 1, cards: [{ kind: 'asset', asset: farAsset }], directories } })
  })
  await page.route('**/api/search?*', async (route) => {
    await route.fulfill({ json: { total: 1, cards: [{ kind: 'asset', asset: farAsset }] } })
  })
  await page.route('**/api/assets/asset-obelisk', async (route) => {
    await route.fulfill({
      json: {
        asset: farAsset,
        actions: actionsFor(farAsset, '/tmp/sample-assets/folder-32/deep/obelisk.png'),
      },
    })
  })
  await page.route('**/api/assets/asset-obelisk/file', async (route) => {
    await route.fulfill({
      contentType: 'image/gif',
      body: Buffer.from('R0lGODlhAQABAAAAACw=', 'base64'),
    })
  })

  await page.goto('/')
  await expect(page.getByRole('button', { name: /obelisk\.png/i })).toBeVisible()
  await expect(page.getByRole('group', { name: 'tokens folder' })).toBeHidden()
  await expect(page.getByRole('group', { name: 'folder-32 folder' })).toBeHidden()

  await page
    .getByRole('group', { name: 'Sample Assets folder' })
    .getByRole('button', { name: 'Expand Sample Assets' })
    .click()
  await page.getByRole('group', { name: 'tokens folder' }).getByRole('button', { name: 'Expand tokens' }).click()
  await expect(page.getByRole('group', { name: 'hidden folder' })).toBeVisible()

  const folderPanel = page.getByLabel('Folder tree')
  await folderPanel.evaluate((element) => {
    element.scrollTop = 0
  })
  const originalScrollTop = await folderPanel.evaluate((element) => element.scrollTop)

  await page.getByRole('button', { name: /obelisk\.png/i }).click()

  await expect.poll(() => folderPanel.evaluate((element) => element.scrollTop)).toBeGreaterThan(originalScrollTop)
  await expect(page.getByRole('group', { name: 'folder-32 folder' })).toBeVisible()
  await expect(page.getByRole('group', { name: 'deep folder' })).toBeVisible()
  await expect(page.getByRole('group', { name: 'hidden folder' })).toBeVisible()
})

test('copies the selected asset FoundryVTT path', async ({ page }) => {
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
  await expect(page.getByLabel('Selected asset')).not.toContainText('maps/castle.png')
  await expect(page.getByRole('group', { name: 'maps folder' })).toHaveClass(/selected-folder/)
  await expect(page.getByRole('note', { name: 'Current selection path' })).toHaveText('castle.png')

  await page.getByRole('button', { name: 'Copy FoundryVTT path' }).click()

  await expect(page.getByRole('button', { name: 'Copied' })).toBeVisible()
  await expect
    .poll(() => page.evaluate(() => (window as unknown as { __copiedText?: string }).__copiedText))
    .toBe('library/Sample Assets/maps/castle.png')
})

test('sizes the selected preview to show the whole image while keeping actions visible', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 })
  const tallAsset = {
    ...sampleAsset,
    asset_id: 'asset-banner',
    relative_path: 'maps/banner.png',
    filename: 'banner.png',
    stem: 'banner',
    thumbnail_width: 200,
    thumbnail_height: 600,
  }

  await page.route('**/api/search-with-directory-counts?*', async (route) => {
    await route.fulfill({
      json: {
        total: 1,
        cards: [{ kind: 'asset', asset: tallAsset }],
        directories: [sampleRootDirectory(), sampleDirectory()],
      },
    })
  })
  await page.route('**/api/search?*', async (route) => {
    await route.fulfill({ json: { total: 1, cards: [{ kind: 'asset', asset: tallAsset }] } })
  })
  await page.route('**/api/assets/asset-banner', async (route) => {
    await route.fulfill({
      json: {
        asset: tallAsset,
        actions: actionsFor(tallAsset, '/tmp/sample-assets/maps/banner.png'),
      },
    })
  })
  await page.route('**/api/assets/asset-banner/file', async (route) => {
    await route.fulfill({
      contentType: 'image/svg+xml',
      body: svgImage(200, 600),
    })
  })

  await page.goto('/')
  await page.getByRole('button', { name: /banner\.png/i }).click()

  const panel = page.getByLabel('Selected asset')
  const preview = panel.locator('.preview')
  const previewImage = preview.locator('img')
  await expect(previewImage).toBeVisible()
  await expect(previewImage).toHaveCSS('object-fit', 'contain')

  const previewBox = await preview.boundingBox()
  const previewImageBox = await previewImage.boundingBox()
  const panelBox = await panel.boundingBox()
  const finalActionBox = await panel.getByRole('button', { name: 'Copy FoundryVTT path' }).boundingBox()
  if (!previewBox || !previewImageBox || !panelBox || !finalActionBox) throw new Error('selected preview layout is missing')

  expect(previewBox.width).toBeLessThanOrEqual(310)
  expect(previewBox.height).toBeGreaterThan(230)
  expect(previewImageBox.height).toBeLessThanOrEqual(previewBox.height)
  expect(previewImageBox.width).toBeLessThan(previewBox.width * 0.45)
  expect(finalActionBox.y + finalActionBox.height).toBeLessThanOrEqual(panelBox.y + panelBox.height)
})

test('copies paths with the legacy clipboard fallback', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: undefined,
    })
    document.execCommand = (command: string) => {
      if (command !== 'copy') return false
      const selected = document.activeElement
      if (selected instanceof HTMLTextAreaElement) {
        ;(window as unknown as { __copiedText?: string }).__copiedText = selected.value
        return true
      }
      return false
    }
  })

  await page.goto('/')
  await page.getByRole('button', { name: /castle\.png/i }).click()

  await page.getByRole('button', { name: 'Copy path' }).click()

  await expect(page.getByRole('button', { name: 'Copied' })).toBeVisible()
  await expect
    .poll(() => page.evaluate(() => (window as unknown as { __copiedText?: string }).__copiedText))
    .toBe('/tmp/sample-assets/maps/castle.png')
})

test('clears transient clipboard errors', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: undefined,
    })
  })

  await page.goto('/')
  await page.getByRole('button', { name: /castle\.png/i }).click()

  await page.getByRole('button', { name: 'Copy', exact: true }).click()

  await expect(page.locator('.status')).toContainText('Image clipboard requires HTTPS or localhost')
  await expect(page.locator('.status')).toContainText('1 card', { timeout: 4000 })
})

test('successful copy clears previous clipboard errors', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: undefined,
    })
    document.execCommand = (command: string) => {
      if (command !== 'copy') return false
      const selected = document.activeElement
      if (selected instanceof HTMLTextAreaElement) {
        ;(window as unknown as { __copiedText?: string }).__copiedText = selected.value
        return true
      }
      return false
    }
  })

  await page.goto('/')
  await page.getByRole('button', { name: /castle\.png/i }).click()

  await page.getByRole('button', { name: 'Copy', exact: true }).click()
  await expect(page.locator('.status')).toContainText('Image clipboard requires HTTPS or localhost')
  await page.getByRole('button', { name: 'Copy path' }).click()

  await expect(page.getByRole('button', { name: 'Copied' })).toBeVisible()
  await expect(page.locator('.status')).toContainText('1 card')
})

test('updates folder quality through the picker and refreshes ranked search results', async ({ page }) => {
  let directoryQuality = 4

  await page.route('**/api/search-with-directory-counts?*', async (route) => {
    await route.fulfill({
      json: {
        total: 1,
        cards: [{ kind: 'asset', asset: sampleAsset }],
        directories: [
          sampleDirectory(directoryQuality, directoryQuality === 4 ? null : directoryQuality),
        ],
      },
    })
  })

  await page.route('**/api/search-profile/settings', async (route) => {
    const body = route.request().postDataJSON() as { quality_override: number | null }
    directoryQuality = body.quality_override ?? 5
    await route.fulfill({
      json: { session: profileSession(true, false) },
    })
  })

  await page.goto('/')
  await expect(page.getByRole('button', { name: 'Folder controls for maps, quality 4' })).toBeVisible()

  const initialSearchRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return url.pathname === '/api/search' && url.searchParams.get('q') === 'castle'
  })
  await page.getByRole('searchbox', { name: 'Search assets' }).fill('castle')
  await initialSearchRequest

  const qualityUpdateRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return url.pathname === '/api/search-profile/settings' && request.method() === 'PATCH'
  })
  const refreshedSearchRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return url.pathname === '/api/search' && url.searchParams.get('q') === 'castle'
  })

  await page.getByRole('button', { name: 'Folder controls for maps, quality 4' }).click()
  await page
    .getByRole('listbox', { name: 'Set quality for maps' })
    .getByRole('option', { name: '8' })
    .click()

  const request = await qualityUpdateRequest
  expectProfileSettingRequest(request, {
    source_id: 'sample',
    directory: 'maps',
    quality_override: 8,
  })
  await refreshedSearchRequest
  await expect(page.getByRole('button', { name: 'Folder controls for maps, quality 8' })).toBeVisible()
})

test('adjusts the focused folder quality with keyboard shortcuts', async ({ page }) => {
  let directoryQuality = 4

  await page.route('**/api/search-with-directory-counts?*', async (route) => {
    await route.fulfill({
      json: {
        total: 1,
        cards: [{ kind: 'asset', asset: sampleAsset }],
        directories: [
          sampleDirectory(directoryQuality, directoryQuality === 4 ? null : directoryQuality),
        ],
      },
    })
  })

  await page.route('**/api/search-profile/settings', async (route) => {
    const body = route.request().postDataJSON() as { quality_override: number | null }
    directoryQuality = body.quality_override ?? 5
    await route.fulfill({
      json: { session: profileSession(true, false) },
    })
  })

  await page.goto('/')
  await page.getByRole('button', { name: 'Folder controls for maps, quality 4' }).focus()

  const qualityUpdateRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return url.pathname === '/api/search-profile/settings' && request.method() === 'PATCH'
  })
  await page.keyboard.press('=')

  const request = await qualityUpdateRequest
  expectProfileSettingRequest(request, {
    source_id: 'sample',
    directory: 'maps',
    quality_override: null,
  })
  await expect(page.getByRole('button', { name: 'Folder controls for maps, quality 5' })).toBeVisible()
})
