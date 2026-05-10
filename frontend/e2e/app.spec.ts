import { expect, test, type Page } from '@playwright/test'

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

function sampleDirectory(quality = 4, qualityOverride: number | null = null) {
  return {
    source_id: 'sample',
    path: 'maps',
    name: 'maps',
    depth: 1,
    asset_count: 1,
    matched_asset_count: 1,
    quality,
    quality_override: qualityOverride,
  }
}

function sampleRootDirectory() {
  return {
    source_id: 'sample',
    path: '',
    name: 'Sample Assets',
    depth: 0,
    asset_count: 1,
    matched_asset_count: 1,
    quality: 5,
    quality_override: null,
  }
}

function filterRulesFromRequest(request: { url: () => string }) {
  const url = new URL(request.url())
  return url.searchParams.getAll('filter_rule').map((rule) => JSON.parse(rule))
}

async function revealSampleFolder(page: Page) {
  await page.getByRole('button', { name: /castle\.png/i }).click()
  await expect(page.getByRole('group', { name: 'maps folder' })).toBeVisible()
}

test.beforeEach(async ({ page }) => {
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
    await route.fulfill({
      json: {
        total: 1,
        cards: [{ kind: 'asset', asset: sampleAsset }],
        directories: [sampleRootDirectory(), sampleDirectory()],
      },
    })
  })

  await page.route('**/api/search?*', async (route) => {
    await route.fulfill({ json: { total: 1, cards: [{ kind: 'asset', asset: sampleAsset }] } })
  })
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
    return url.pathname === '/api/search-with-directory-counts' && url.searchParams.get('q') === 'castle'
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
      url.pathname === '/api/search-with-directory-counts' &&
      url.searchParams.get('q') === 'castle' &&
      url.searchParams.get('mode') === 'domain'
    )
  })

  await page.getByRole('tab', { name: 'Domain' }).click()
  await domainSearchRequest

  await expect(page.getByRole('tab', { name: 'Domain' })).toHaveAttribute('aria-selected', 'true')
})

test('sends sidebar whitelist and blacklist rules with folder shortcuts', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('button', { name: /castle\.png/i })).toBeVisible()
  await revealSampleFolder(page)

  const whitelistedAssetsRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return (
      url.pathname === '/api/search-with-directory-counts' &&
      url.searchParams.getAll('filter_rule').some((rule) => rule.includes('"state":"whitelist"'))
    )
  })
  await page.getByRole('group', { name: 'maps folder' }).getByRole('button', { name: 'maps', exact: true }).click({
    modifiers: ['Shift'],
  })

  const whitelistRequest = await whitelistedAssetsRequest
  const whitelistUrl = new URL(whitelistRequest.url())
  expect(whitelistUrl.searchParams.has('directory_prefix')).toBe(false)
  expect(filterRulesFromRequest(whitelistRequest)).toEqual([
    { source_id: 'sample', path: 'maps', state: 'whitelist' },
  ])

  const blacklistedAssetsRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return (
      url.pathname === '/api/search-with-directory-counts' &&
      url.searchParams.getAll('filter_rule').some((rule) => rule.includes('"state":"blacklist"'))
    )
  })
  await page.getByRole('group', { name: 'maps folder' }).getByRole('button', { name: 'maps', exact: true }).click({
    modifiers: ['Alt'],
  })

  expect(filterRulesFromRequest(await blacklistedAssetsRequest)).toEqual([
    { source_id: 'sample', path: 'maps', state: 'blacklist' },
  ])
  await expect(page.getByRole('group', { name: 'maps folder' })).toBeVisible()
})

test('combines folder filtering and quality in one control popover', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('button', { name: /castle\.png/i })).toBeVisible()
  await revealSampleFolder(page)

  await page.getByRole('button', { name: 'Folder controls for maps, quality 4' }).click()
  await expect(page.getByRole('group', { name: 'Folder controls for maps', exact: true })).toBeVisible()

  const focusedAssetsRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return (
      url.pathname === '/api/search-with-directory-counts' &&
      url.searchParams.getAll('filter_rule').some((rule) => rule.includes('"state":"whitelist"'))
    )
  })
  await page.getByRole('button', { name: 'Focus maps' }).click()

  expect(filterRulesFromRequest(await focusedAssetsRequest)).toEqual([
    { source_id: 'sample', path: 'maps', state: 'whitelist' },
  ])
})

test('marks folders that contain custom descendant folder settings', async ({ page }) => {
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
            quality: 8,
            quality_override: 8,
          },
        ],
      },
    })
  })

  await page.goto('/')
  await expect(page.getByRole('button', { name: /castle\.png/i })).toBeVisible()

  await expect(
    page.getByRole('group', { name: 'Sample Assets folder' }).getByRole('button', { name: /quality 5/ })
  ).toHaveAttribute('title', 'Contains custom folder settings')

  await revealSampleFolder(page)

  await expect(
    page.getByRole('group', { name: 'maps folder' }).getByRole('button', { name: /quality 4/ })
  ).toHaveAttribute('title', 'Contains custom folder settings')
  await expect(
    page.getByRole('group', { name: 'sub folder' }).getByRole('button', { name: /quality 8/ })
  ).not.toHaveAttribute('title', 'Contains custom folder settings')
})

test('folder quality button shortcuts toggle folder rules without opening the menu', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('button', { name: /castle\.png/i })).toBeVisible()
  await revealSampleFolder(page)

  const focusedAssetsRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return (
      url.pathname === '/api/search-with-directory-counts' &&
      url.searchParams.getAll('filter_rule').some((rule) => rule.includes('"state":"whitelist"'))
    )
  })
  await page.getByRole('button', { name: 'Folder controls for maps, quality 4' }).click({ modifiers: ['Shift'] })

  expect(filterRulesFromRequest(await focusedAssetsRequest)).toEqual([
    { source_id: 'sample', path: 'maps', state: 'whitelist' },
  ])
  await expect(page.getByRole('group', { name: 'Folder controls for maps', exact: true })).toHaveCount(0)

  const blockedAssetsRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return (
      url.pathname === '/api/search-with-directory-counts' &&
      url.searchParams.getAll('filter_rule').some((rule) => rule.includes('"state":"blacklist"'))
    )
  })
  await page.getByRole('button', { name: 'Folder controls for maps, focused, quality 4' }).click({
    modifiers: ['Alt'],
  })

  expect(filterRulesFromRequest(await blockedAssetsRequest)).toEqual([
    { source_id: 'sample', path: 'maps', state: 'blacklist' },
  ])
  await expect(page.getByRole('group', { name: 'Folder controls for maps', exact: true })).toHaveCount(0)
})

test('keeps sidebar rules across search text and lane changes', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('button', { name: /castle\.png/i })).toBeVisible()
  await revealSampleFolder(page)
  await page.getByRole('group', { name: 'maps folder' }).getByRole('button', { name: 'maps', exact: true }).click({
    modifiers: ['Shift'],
  })

  const filteredSearchRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return (
      url.pathname === '/api/search-with-directory-counts' &&
      url.searchParams.get('q') === 'castle' &&
      url.searchParams.getAll('filter_rule').length === 1
    )
  })
  await page.getByRole('searchbox', { name: 'Search assets' }).fill('castle')
  expect(filterRulesFromRequest(await filteredSearchRequest)).toEqual([
    { source_id: 'sample', path: 'maps', state: 'whitelist' },
  ])

  const domainSearchRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return (
      url.pathname === '/api/search-with-directory-counts' &&
      url.searchParams.get('q') === 'castle' &&
      url.searchParams.get('mode') === 'domain' &&
      url.searchParams.getAll('filter_rule').length === 1
    )
  })
  await page.getByRole('tab', { name: 'Domain' }).click()

  expect(filterRulesFromRequest(await domainSearchRequest)).toEqual([
    { source_id: 'sample', path: 'maps', state: 'whitelist' },
  ])
})

test('moves whitelist focus within the same subtree', async ({ page }) => {
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
            quality: 5,
            quality_override: null,
          },
          {
            source_id: 'sample',
            path: 'tokens',
            name: 'tokens',
            depth: 1,
            asset_count: 1,
            matched_asset_count: 1,
            quality: 5,
            quality_override: null,
          },
        ],
      },
    })
  })

  await page.goto('/')
  await expect(page.getByRole('button', { name: /castle\.png/i })).toBeVisible()
  await revealSampleFolder(page)

  await page.getByRole('group', { name: 'maps folder' }).getByRole('button', { name: 'maps', exact: true }).click({
    modifiers: ['Shift'],
  })

  const childWhitelistRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    const rules = url.searchParams.getAll('filter_rule')
    return (
      url.pathname === '/api/search-with-directory-counts' &&
      rules.length === 1 &&
      rules[0].includes('"path":"maps/sub"')
    )
  })
  await page.getByRole('group', { name: 'sub folder' }).getByRole('button', { name: 'sub', exact: true }).click({
    modifiers: ['Shift'],
  })

  expect(filterRulesFromRequest(await childWhitelistRequest)).toEqual([
    { source_id: 'sample', path: 'maps/sub', state: 'whitelist' },
  ])
  await expect(
    page.getByRole('group', { name: 'maps folder' }).getByRole('button', { name: /quality 4/ })
  ).toHaveAttribute('title', 'Contains custom folder settings')
  await expect(
    page.getByRole('group', { name: 'sub folder' }).getByRole('button', { name: /quality 5/ })
  ).not.toHaveAttribute('title', 'Contains custom folder settings')

  const siblingWhitelistRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return (
      url.pathname === '/api/search-with-directory-counts' &&
      url.searchParams.getAll('filter_rule').some((rule) => rule.includes('"path":"tokens"'))
    )
  })
  await page.getByRole('group', { name: 'tokens folder' }).getByRole('button', { name: 'tokens', exact: true }).click({
    modifiers: ['Shift'],
  })

  expect(filterRulesFromRequest(await siblingWhitelistRequest)).toEqual([
    { source_id: 'sample', path: 'maps/sub', state: 'whitelist' },
    { source_id: 'sample', path: 'tokens', state: 'whitelist' },
  ])
})

test('highlights the hovered asset folder path', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('button', { name: /castle\.png/i })).toBeVisible()
  await revealSampleFolder(page)

  await page.getByRole('button', { name: /castle\.png/i }).hover()

  await expect(page.getByRole('group', { name: 'Sample Assets folder' })).toHaveClass(/path-highlight/)
  await expect(page.getByRole('group', { name: 'maps folder' })).toHaveClass(/direct-highlight/)
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
            quality: 5,
            quality_override: null,
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

  await tile.hover()
  await expect.poll(() => page.evaluate(() => (window as unknown as { __audioEvents: string[] }).__audioEvents)).toContain(
    'play:/api/assets/asset-thunder/file:muted=true:volume=0.5'
  )
  const audioCard = page.getByRole('group', { name: 'Audio asset card for thunder.mp3' })
  await expect(audioCard.getByRole('button', { name: 'Unmute preview' })).toBeVisible()
  await expect(audioCard.getByRole('slider', { name: 'Preview volume' })).toBeHidden()
  await expect(page.getByLabel('Hovered asset preview').getByRole('button', { name: 'Unmute preview' })).toHaveCount(0)

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
  await tile.click()
  await expect.poll(() => page.evaluate(() => (window as unknown as { __audioEvents: string[] }).__audioEvents)).toContain(
    'pause:/api/assets/asset-thunder/file'
  )
  const selectedPanel = page.getByLabel('Selected asset')
  await expect(selectedPanel.locator('audio[controls]')).toHaveAttribute('src', '/api/assets/asset-thunder/file')
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
      quality: 5,
      quality_override: null,
    },
    {
      source_id: 'sample',
      path: 'tokens/hidden',
      name: 'hidden',
      depth: 2,
      asset_count: 1,
      matched_asset_count: 1,
      quality: 5,
      quality_override: null,
    },
    ...Array.from({ length: 40 }, (_, index) => ({
      source_id: 'sample',
      path: `folder-${String(index).padStart(2, '0')}`,
      name: `folder-${String(index).padStart(2, '0')}`,
      depth: 1,
      asset_count: 1,
      matched_asset_count: 1,
      quality: 5,
      quality_override: null,
    })),
    {
      source_id: 'sample',
      path: 'folder-32/deep',
      name: 'deep',
      depth: 2,
      asset_count: 1,
      matched_asset_count: 1,
      quality: 5,
      quality_override: null,
    },
  ]

  await page.route('**/api/search-with-directory-counts?*', async (route) => {
    await route.fulfill({ json: { total: 1, cards: [{ kind: 'asset', asset: farAsset }], directories } })
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
    .getByRole('button', { name: 'Sample Assets', exact: true })
    .click()
  await page.getByRole('group', { name: 'tokens folder' }).getByRole('button', { name: 'tokens', exact: true }).click()
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
  await expect(page.getByLabel('Selected asset')).toContainText('maps/castle.png')
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
      contentType: 'image/gif',
      body: Buffer.from('R0lGODlhAQABAAAAACw=', 'base64'),
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
  const panelBox = await panel.boundingBox()
  const finalActionBox = await panel.getByRole('button', { name: 'Copy FoundryVTT path' }).boundingBox()
  if (!previewBox || !panelBox || !finalActionBox) throw new Error('selected preview layout is missing')

  expect(previewBox.width).toBeLessThanOrEqual(310)
  expect(previewBox.height).toBeGreaterThan(230)
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

  await page.route('**/api/folder-quality', async (route) => {
    const body = route.request().postDataJSON() as { quality: number }
    directoryQuality = body.quality
    await route.fulfill({
      json: { directory: sampleDirectory(directoryQuality, directoryQuality) },
    })
  })

  await page.goto('/')
  await expect(page.getByRole('button', { name: 'Folder controls for maps, quality 4' })).toBeVisible()

  const initialSearchRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return url.pathname === '/api/search-with-directory-counts' && url.searchParams.get('q') === 'castle'
  })
  await page.getByRole('searchbox', { name: 'Search assets' }).fill('castle')
  await initialSearchRequest

  const qualityUpdateRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return url.pathname === '/api/folder-quality' && request.method() === 'PUT'
  })
  const refreshedSearchRequest = page.waitForRequest((request) => {
    const url = new URL(request.url())
    return url.pathname === '/api/search-with-directory-counts' && url.searchParams.get('q') === 'castle'
  })

  await page.getByRole('button', { name: 'Folder controls for maps, quality 4' }).click()
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

  await page.route('**/api/folder-quality', async (route) => {
    const body = route.request().postDataJSON() as { quality: number }
    directoryQuality = body.quality
    await route.fulfill({
      json: { directory: sampleDirectory(directoryQuality, directoryQuality) },
    })
  })

  await page.goto('/')
  await page.getByRole('button', { name: 'Folder controls for maps, quality 4' }).focus()

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
  await expect(page.getByRole('button', { name: 'Folder controls for maps, quality 5' })).toBeVisible()
})
