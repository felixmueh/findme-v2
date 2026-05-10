<script lang="ts">
  import { tick } from 'svelte'
  import {
    Copy,
    Ban,
    Check,
    Download,
    ExternalLink,
    FileQuestion,
    Film,
    Folder,
    ChevronRight,
    Image as ImageIcon,
    Layers2,
    LoaderCircle,
    Search,
  } from 'lucide-svelte'
  import {
    type Asset,
    type CollectionCard,
    type ResultCard,
    type VisibleEntry,
    cardContainsAsset,
    collectionColor,
    visibleEntriesForCards,
  } from './lib/result-cards'
  import SearchLaneTabs, { type SearchMode } from './lib/SearchLaneTabs.svelte'

  type DirectoryNode = {
    source_id: string
    path: string
    name: string
    depth: number
    asset_count: number
    matched_asset_count: number
    quality: number
    quality_override: number | null
  }

  type FilterRuleState = 'whitelist' | 'blacklist'

  type ActionProfile = {
    id: string
    label: string
    value: string
  }

  type AssetDetail = {
    asset: Asset
    actions: {
      primary: ActionProfile
      profiles: ActionProfile[]
      source_relative: string
      absolute_path: string
      open_url: string
      download_url: string
    }
  }

  let query = $state('')
  let searchMode = $state<SearchMode>('strict')
  let filterRules = $state<Map<string, FilterRuleState>>(new Map())
  let directories = $state<DirectoryNode[]>([])
  let collapsedDirectories = $state<Set<string>>(new Set())
  let cards = $state<ResultCard[]>([])
  let expandedCollections = $state<Set<string>>(new Set())
  let selected = $state<AssetDetail | null>(null)
  let hoveredAsset = $state<Asset | null>(null)
  let hoveredCollection = $state<CollectionCard | null>(null)
  let qualityTargetKey = $state('')
  let qualityHoverKey = $state('')
  let cyclingCollectionKey = $state('')
  let cyclingVariantIndex = $state(0)
  let loading = $state(false)
  let loadingMore = $state(false)
  let hasMoreAssets = $state(false)
  let resultTotal = $state(0)
  let error = $state('')
  let copied = $state('')
  let debounceTimer: number | undefined
  let directoryDebounceTimer: number | undefined
  let requestId = 0
  let searchRevision = 0
  let collectionCycleTimer: number | undefined
  let assetRequestController: AbortController | undefined
  let directoryRequestController: AbortController | undefined
  let searchInput: HTMLInputElement
  let tileGridElement = $state<HTMLDivElement>()
  let folderPanelElement = $state<HTMLElement>()
  let loadMoreTrigger = $state<HTMLDivElement>()
  let folderHoverRestoreScrollTop: number | null = null
  let hasInitializedFolderCollapse = false

  const pageSize = 100
  const searchSessionId =
    typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random()}`
  const directorySearchSessionId = `${searchSessionId}:directories`

  const selectedAssetId = $derived(selected?.asset.asset_id ?? '')
  const selectedDirectoryKey = $derived(
    selected ? directoryKey({ source_id: selected.asset.source_id, path: selected.asset.directory }) : ''
  )
  const visibleEntries = $derived.by(() => visibleEntriesForCards(cards, expandedCollections))
  const selectedIndex = $derived(
    visibleEntries.findIndex((entry) => entry.kind === 'asset' && entry.asset.asset_id === selectedAssetId)
  )
  const hoverPreviewAsset = $derived(
    hoveredCollection ? collectionDisplayAsset(hoveredCollection) : hoveredAsset
  )
  const resultLabel = $derived(`${resultTotal} ${resultTotal === 1 ? 'card' : 'cards'}`)
  const visibleDirectories = $derived(directories.filter((directory) => isDirectoryVisible(directory)))
  const serializedFilterRules = $derived.by(() =>
    Array.from(filterRules.entries()).map(([key, state]) => {
      const separatorIndex = key.indexOf('\u0000')
      const source_id = key.slice(0, separatorIndex)
      const path = key.slice(separatorIndex + 1)
      return { source_id, path, state }
    })
  )
  const hoveredAssetAncestorKeys = $derived.by(() => {
    const asset = hoverPreviewAsset
    const keys = new Set<string>()
    if (!asset) return keys
    keys.add(directoryKey({ source_id: asset.source_id, path: '' }))
    if (!asset.directory) return keys
    const parts = asset.directory.split('/')
    for (let index = 1; index <= parts.length; index += 1) {
      keys.add(directoryKey({ source_id: asset.source_id, path: parts.slice(0, index).join('/') }))
    }
    return keys
  })
  const directHoveredDirectoryKey = $derived.by(() => {
    const asset = hoverPreviewAsset
    return asset ? directoryKey({ source_id: asset.source_id, path: asset.directory }) : ''
  })
  const qualityTargetDirectory = $derived.by(
    () =>
      directories.find(
        (directory) => directoryKey(directory) === (qualityHoverKey || qualityTargetKey)
      ) ?? null
  )
  const directoryPathsWithChildren = $derived.by(() => directoryParentKeys(directories))
  const directoriesWithDescendantFolderSettings = $derived.by(() =>
    descendantFolderSettingKeys(directories, filterRules)
  )

  $effect(() => {
    void loadDirectories()
  })

  $effect(() => {
    query
    searchMode
    filterRules
    const revision = ++searchRevision
    window.clearTimeout(debounceTimer)
    debounceTimer = window.setTimeout(() => {
      void loadAssets(revision)
    }, 150)
  })

  $effect(() => {
    query
    searchMode
    filterRules
    const revision = searchRevision
    window.clearTimeout(directoryDebounceTimer)
    directoryDebounceTimer = window.setTimeout(() => {
      void loadDirectories(revision)
    }, 150)
  })

  $effect(() => {
    if (!tileGridElement || !loadMoreTrigger || !hasMoreAssets) return
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          void loadMoreAssets()
        }
      },
      { root: tileGridElement, rootMargin: '360px' }
    )
    observer.observe(loadMoreTrigger)
    return () => observer.disconnect()
  })

  $effect(() => {
    const key = directHoveredDirectoryKey
    const panel = folderPanelElement
    if (!panel) return
    if (!key) {
      if (folderHoverRestoreScrollTop !== null) {
        panel.scrollTop = folderHoverRestoreScrollTop
        folderHoverRestoreScrollTop = null
      }
      return
    }
    void scrollHoveredDirectoryIntoView(key)
  })

  function setSearchMode(mode: SearchMode) {
    searchMode = mode
  }

  async function loadDirectories(revision?: number) {
    directoryRequestController?.abort()
    const controller = new AbortController()
    directoryRequestController = controller
    const params = new URLSearchParams()
    appendFilterRules(params)
    const trimmed = query.trim()
    if (trimmed) {
      params.set('q', trimmed)
      params.set('mode', searchMode)
      if (revision !== undefined) {
        params.set('search_session', directorySearchSessionId)
        params.set('search_revision', String(revision))
      }
    }
    try {
      const response = await fetch(`/api/directories?${params}`, { signal: controller.signal })
      if (response.status === 204) return
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      const body = await response.json()
      if (directoryRequestController === controller) {
        const nextDirectories = body.directories as DirectoryNode[]
        directories = nextDirectories
        if (!hasInitializedFolderCollapse) {
          collapsedDirectories = initiallyCollapsedDirectoryKeys(nextDirectories)
          hasInitializedFolderCollapse = true
        }
      }
    } catch (caught) {
      if (!isAbortError(caught) && directoryRequestController === controller) {
        error = 'Could not load folders'
      }
    } finally {
      if (directoryRequestController === controller) {
        directoryRequestController = undefined
      }
    }
  }

  async function loadAssets(revision = searchRevision) {
    await loadAssetPage({ offset: 0, append: false, revision })
  }

  async function loadMoreAssets() {
    if (!hasMoreAssets || loading || loadingMore) return
    await loadAssetPage({ offset: cards.length, append: true, revision: searchRevision })
  }

  async function loadAssetPage({ offset, append, revision }: { offset: number; append: boolean; revision: number }) {
    const currentRequest = ++requestId
    assetRequestController?.abort()
    const controller = new AbortController()
    assetRequestController = controller
    const params = new URLSearchParams()
    params.set('limit', String(pageSize))
    params.set('offset', String(offset))
    appendFilterRules(params)
    const trimmed = query.trim()
    const path = trimmed ? '/api/search' : '/api/assets'
    if (trimmed) {
      params.set('q', trimmed)
      params.set('mode', searchMode)
      params.set('search_session', searchSessionId)
      params.set('search_revision', String(revision))
    }

    if (append) {
      loadingMore = true
    } else {
      loading = true
      hasMoreAssets = false
      resultTotal = 0
    }
    error = ''
    try {
      const response = await fetch(`${path}?${params}`, { signal: controller.signal })
      if (response.status === 204) return
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      const body = await response.json()
      if (currentRequest !== requestId) return
      const nextCards = body.cards as ResultCard[]
      resultTotal = Number(body.total ?? nextCards.length)
      cards = append ? [...cards, ...nextCards] : nextCards
      hasMoreAssets = cards.length < resultTotal
      if (!append) {
        expandedCollections = new Set()
        stopCollectionCycle()
      }
      if (!append && cards.length === 0) {
        selected = null
      } else if (!append && selected && !cards.some((card) => cardContainsAsset(card, selectedAssetId))) {
        selected = null
      }
    } catch (caught) {
      if (!isAbortError(caught) && currentRequest === requestId) error = 'Search failed'
    } finally {
      if (currentRequest === requestId) {
        loading = false
        loadingMore = false
      }
      if (assetRequestController === controller) {
        assetRequestController = undefined
      }
    }
  }

  function isAbortError(caught: unknown) {
    return caught instanceof DOMException && caught.name === 'AbortError'
  }

  async function selectAsset(asset: Asset) {
    const response = await fetch(`/api/assets/${asset.asset_id}`)
    if (!response.ok) {
      error = 'Could not load asset'
      return
    }
    const detail = (await response.json()) as AssetDetail
    selected = detail
    revealSelectedAssetDirectory(detail.asset)
  }

  async function copyPrimary() {
    if (!selected) return
    await navigator.clipboard.writeText(selected.actions.primary.value)
    copied = selected.asset.asset_id
    window.setTimeout(() => {
      copied = ''
    }, 1200)
  }

  function chooseRelative(offset: number) {
    if (visibleEntries.length === 0) return
    const fallback = selectedIndex < 0 ? 0 : selectedIndex
    let next = Math.min(Math.max(fallback + offset, 0), visibleEntries.length - 1)
    const step = Math.sign(offset) || 1
    while (visibleEntries[next]?.kind === 'collection-header') {
      const candidate = next + step
      if (candidate < 0 || candidate >= visibleEntries.length) return
      next = candidate
    }
    const entry = visibleEntries[next]
    if (entry.kind === 'collection') {
      expandCollection(entry.collection)
      return
    }
    if (entry.kind === 'collection-header') return
    void selectAsset(entry.asset)
  }

  function chooseGridRelative(direction: 'left' | 'right' | 'up' | 'down') {
    const columns = gridColumnCount()
    const offsets = {
      left: -1,
      right: 1,
      up: -columns,
      down: columns,
    }
    chooseRelative(offsets[direction])
  }

  function gridColumnCount() {
    if (!tileGridElement) return 1
    const columns = window.getComputedStyle(tileGridElement).gridTemplateColumns
    if (!columns || columns === 'none') return 1
    return Math.max(columns.split(' ').filter(Boolean).length, 1)
  }

  async function refreshSearchFromInput() {
    window.clearTimeout(debounceTimer)
    const revision = ++searchRevision
    await loadAssetPage({ offset: 0, append: false, revision })
  }

  function handleKeydown(event: KeyboardEvent) {
    const target = event.target as HTMLElement | null
    const isTyping =
      target instanceof HTMLInputElement ||
      target instanceof HTMLTextAreaElement ||
      target instanceof HTMLSelectElement ||
      target?.isContentEditable

    if (event.key === '/' && !isTyping) {
      event.preventDefault()
      searchInput?.focus()
      searchInput?.select()
      return
    }
    if (event.key === 'Enter' && target === searchInput) {
      event.preventDefault()
      void refreshSearchFromInput()
      return
    }
    if (event.key === 'Escape') {
      event.preventDefault()
      selected = null
      qualityTargetKey = ''
      return
    }
    if (handleFolderQualityShortcut(event)) return
    if (isTyping) return
    if (event.key === 'ArrowRight' || event.key === 'l') {
      event.preventDefault()
      chooseGridRelative('right')
    } else if (event.key === 'ArrowLeft' || event.key === 'h') {
      event.preventDefault()
      chooseGridRelative('left')
    } else if (event.key === 'ArrowDown' || event.key === 'j') {
      event.preventDefault()
      chooseGridRelative('down')
    } else if (event.key === 'ArrowUp' || event.key === 'k') {
      event.preventDefault()
      chooseGridRelative('up')
    } else if (event.key === 'Enter' && selected) {
      event.preventDefault()
      void copyPrimary()
    }
  }

  function assetIcon(asset: Asset) {
    if (asset.media_type === 'image') return ImageIcon
    if (asset.media_type === 'video') return Film
    return FileQuestion
  }

  function formatBytes(bytes: number) {
    if (bytes < 1024) return `${bytes} B`
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  }

  function directoryKey(directory: Pick<DirectoryNode, 'source_id' | 'path'>) {
    return `${directory.source_id}\u0000${directory.path}`
  }

  function directoryDomKey(directory: Pick<DirectoryNode, 'source_id' | 'path'>) {
    return `${encodeURIComponent(directory.source_id)}:${encodeURIComponent(directory.path)}`
  }

  function directoryDomKeyFromKey(key: string) {
    const separatorIndex = key.indexOf('\u0000')
    return `${encodeURIComponent(key.slice(0, separatorIndex))}:${encodeURIComponent(key.slice(separatorIndex + 1))}`
  }

  async function scrollHoveredDirectoryIntoView(key: string) {
    await tick()
    const panel = folderPanelElement
    if (!panel || directHoveredDirectoryKey !== key) return
    const row = panel.querySelector<HTMLElement>(`[data-directory-key="${directoryDomKeyFromKey(key)}"]`)
    if (!row) return
    const panelRect = panel.getBoundingClientRect()
    const rowRect = row.getBoundingClientRect()
    if (rowRect.top >= panelRect.top && rowRect.bottom <= panelRect.bottom) return
    if (folderHoverRestoreScrollTop === null) {
      folderHoverRestoreScrollTop = panel.scrollTop
    }
    row.scrollIntoView({ block: 'nearest' })
  }

  function revealSelectedAssetDirectory(asset: Asset) {
    unfoldAssetDirectoryPath(asset)
    void scrollSelectedDirectoryIntoView(asset)
  }

  function unfoldAssetDirectoryPath(asset: Asset) {
    const next = new Set(collapsedDirectories)
    let changed = false
    for (const key of directoryAncestorKeysForAsset(asset)) {
      changed = next.delete(key) || changed
    }
    if (changed) collapsedDirectories = next
  }

  async function scrollSelectedDirectoryIntoView(asset: Asset) {
    const key = directoryKey({ source_id: asset.source_id, path: asset.directory })
    await tick()
    const panel = folderPanelElement
    if (!panel || selected?.asset.asset_id !== asset.asset_id) return
    const row = panel.querySelector<HTMLElement>(`[data-directory-key="${directoryDomKeyFromKey(key)}"]`)
    row?.scrollIntoView({ block: 'nearest' })
  }

  function directoryAncestorKeysForAsset(asset: Asset) {
    const keys = [directoryKey({ source_id: asset.source_id, path: '' })]
    if (!asset.directory) return keys
    const parts = asset.directory.split('/')
    for (let index = 1; index <= parts.length; index += 1) {
      keys.push(directoryKey({ source_id: asset.source_id, path: parts.slice(0, index).join('/') }))
    }
    return keys
  }

  function directoryParentKeys(directoryList: DirectoryNode[]) {
    const paths = new Set<string>()
    for (const directory of directoryList) {
      if (!directory.path) continue
      const parentPath = directory.path.includes('/')
        ? directory.path.slice(0, directory.path.lastIndexOf('/'))
        : ''
      paths.add(directoryKey({ source_id: directory.source_id, path: parentPath }))
    }
    return paths
  }

  function initiallyCollapsedDirectoryKeys(directoryList: DirectoryNode[]) {
    const parentKeys = directoryParentKeys(directoryList)
    return new Set(
      directoryList
        .map((directory) => directoryKey(directory))
        .filter((key) => parentKeys.has(key))
    )
  }

  function descendantFolderSettingKeys(directoryList: DirectoryNode[], rules: Map<string, FilterRuleState>) {
    const keys = new Set<string>()
    for (const directory of directoryList) {
      if (directory.quality_override === null) continue
      for (const key of directoryAncestorKeys(directory)) {
        keys.add(key)
      }
    }
    for (const key of rules.keys()) {
      const directory = nodeKeyParts(key)
      for (const ancestorKey of directoryAncestorKeys({ source_id: directory.sourceId, path: directory.path })) {
        keys.add(ancestorKey)
      }
    }
    return keys
  }

  function directoryAncestorKeys(directory: Pick<DirectoryNode, 'source_id' | 'path'>) {
    if (!directory.path) return []
    const keys = [directoryKey({ source_id: directory.source_id, path: '' })]
    const parts = directory.path.split('/')
    for (let index = 1; index < parts.length; index += 1) {
      keys.push(directoryKey({ source_id: directory.source_id, path: parts.slice(0, index).join('/') }))
    }
    return keys
  }

  function appendFilterRules(params: URLSearchParams) {
    for (const rule of serializedFilterRules) {
      params.append('filter_rule', JSON.stringify(rule))
    }
  }

  function directoryRuleState(directory: DirectoryNode) {
    return filterRules.get(directoryKey(directory)) ?? null
  }

  function folderControlLabel(directory: DirectoryNode, ruleState: FilterRuleState | null) {
    const state = ruleState === 'whitelist' ? 'focused, ' : ruleState === 'blacklist' ? 'blocked, ' : ''
    return `Folder controls for ${directory.name}, ${state}quality ${directory.quality}`
  }

  function nodeKeyParts(key: string) {
    const separatorIndex = key.indexOf('\u0000')
    return {
      sourceId: key.slice(0, separatorIndex),
      path: key.slice(separatorIndex + 1),
    }
  }

  function pathsShareSubtree(first: string, second: string) {
    return first === second || isPathAncestor(first, second) || isPathAncestor(second, first)
  }

  function isPathAncestor(ancestor: string, path: string) {
    return ancestor === '' ? path !== '' : path.startsWith(`${ancestor}/`)
  }

  function toggleDirectoryRule(directory: DirectoryNode, state: FilterRuleState) {
    const key = directoryKey(directory)
    const next = new Map(filterRules)
    if (next.get(key) === state) {
      next.delete(key)
    } else {
      if (state === 'whitelist') {
        const target = nodeKeyParts(key)
        for (const [existingKey, existingState] of next) {
          if (existingState !== 'whitelist') continue
          const existing = nodeKeyParts(existingKey)
          if (existing.sourceId === target.sourceId && pathsShareSubtree(existing.path, target.path)) {
            next.delete(existingKey)
          }
        }
      }
      next.set(key, state)
    }
    filterRules = next
  }

  function setDirectoryRule(event: MouseEvent, directory: DirectoryNode, state: FilterRuleState) {
    event.stopPropagation()
    toggleDirectoryRule(directory, state)
  }

  function clearFilterRules() {
    filterRules = new Map()
  }

  function hasChildren(directory: DirectoryNode) {
    return directoryPathsWithChildren.has(directoryKey(directory))
  }

  function isCollapsed(directory: DirectoryNode) {
    return collapsedDirectories.has(directoryKey(directory))
  }

  function isDirectoryVisible(directory: DirectoryNode) {
    if (!directory.path) return true
    if (collapsedDirectories.has(directoryKey({ source_id: directory.source_id, path: '' }))) return false
    const parts = directory.path.split('/')
    for (let index = 1; index < parts.length; index += 1) {
      if (
        collapsedDirectories.has(
          directoryKey({ source_id: directory.source_id, path: parts.slice(0, index).join('/') })
        )
      ) {
        return false
      }
    }
    return true
  }

  function handleDirectoryClick(event: MouseEvent, directory: DirectoryNode) {
    if (event.shiftKey || event.altKey) {
      event.preventDefault()
      event.stopPropagation()
      toggleDirectoryRule(directory, event.shiftKey ? 'whitelist' : 'blacklist')
      return
    }
    if (!hasChildren(directory)) return
    const next = new Set(collapsedDirectories)
    const key = directoryKey(directory)
    if (next.has(key)) {
      next.delete(key)
    } else {
      next.add(key)
    }
    collapsedDirectories = next
  }

  async function setFolderQuality(directory: DirectoryNode, quality: number) {
    const nextQuality = Math.max(0, Math.min(10, quality))
    if (nextQuality === directory.quality && directory.quality_override === null) return
    const response = await fetch('/api/folder-quality', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source_id: directory.source_id,
        directory: directory.path,
        quality: nextQuality,
      }),
    })
    if (!response.ok) {
      error = 'Could not update folder quality'
      return
    }
    const revision = ++searchRevision
    await loadDirectories(revision)
    await loadAssetPage({ offset: 0, append: false, revision })
  }

  function changeFolderQuality(event: MouseEvent, directory: DirectoryNode, quality: number) {
    event.stopPropagation()
    void setFolderQuality(directory, quality)
    qualityTargetKey = ''
  }

  function handleFolderQualityShortcut(event: KeyboardEvent) {
    const directory = qualityTargetDirectory
    if (!directory) return false
    let quality: number | null = null
    if (event.key === '+' || event.key === '=') {
      quality = directory.quality + 1
    } else if (event.key === '-') {
      quality = directory.quality - 1
    } else if (/^\d$/.test(event.key)) {
      quality = Number(event.key)
    }
    if (quality === null) return false
    event.preventDefault()
    void setFolderQuality(directory, quality)
    return true
  }

  function setQualityHover(directory: DirectoryNode) {
    qualityHoverKey = directoryKey(directory)
  }

  function clearQualityHover(directory: DirectoryNode) {
    const key = directoryKey(directory)
    if (qualityHoverKey === key) qualityHoverKey = ''
  }

  function toggleQualityPicker(event: MouseEvent, directory: DirectoryNode) {
    event.stopPropagation()
    if (event.shiftKey || event.altKey) {
      event.preventDefault()
      toggleDirectoryRule(directory, event.shiftKey ? 'whitelist' : 'blacklist')
      qualityTargetKey = ''
      return
    }
    const key = directoryKey(directory)
    qualityTargetKey = qualityTargetKey === key ? '' : key
  }

  function openAsset(asset: Asset) {
    window.open(`/api/assets/${asset.asset_id}/file`, '_blank', 'noopener')
  }

  function handleAssetAuxClick(event: MouseEvent, asset: Asset) {
    if (event.button !== 1) return
    event.preventDefault()
    openAsset(asset)
  }

  function entryAsset(entry: VisibleEntry) {
    return entry.kind === 'asset' ? entry.asset : entry.collection.representative
  }

  function visibleEntryKey(entry: VisibleEntry) {
    if (entry.kind === 'collection') return `collection:${entry.collection.collection_key}`
    if (entry.kind === 'collection-header') return `collection-header:${entry.collection.collection_key}`
    return `${entry.collection_key ?? 'asset'}:${entry.asset.asset_id}`
  }

  function expandCollection(collection: CollectionCard) {
    const next = new Set(expandedCollections)
    next.add(collection.collection_key)
    expandedCollections = next
    stopCollectionCycle()
  }

  function foldCollection(collectionKey: string) {
    const next = new Set(expandedCollections)
    next.delete(collectionKey)
    expandedCollections = next
  }

  function collectionDisplayAsset(collection: CollectionCard) {
    if (cyclingCollectionKey !== collection.collection_key) return collection.representative
    return collection.variants[cyclingVariantIndex % collection.variants.length] ?? collection.representative
  }

  function startCollectionCycle(collection: CollectionCard) {
    if (collection.variants.length < 2) return
    window.clearInterval(collectionCycleTimer)
    hoveredAsset = null
    hoveredCollection = collection
    cyclingCollectionKey = collection.collection_key
    cyclingVariantIndex = Math.max(
      collection.variants.findIndex((asset) => asset.asset_id === collection.representative.asset_id),
      0
    )
    collectionCycleTimer = window.setInterval(() => {
      cyclingVariantIndex = (cyclingVariantIndex + 1) % collection.variants.length
    }, 600)
  }

  function stopCollectionCycle(collectionKey?: string) {
    if (collectionKey && cyclingCollectionKey !== collectionKey) return
    window.clearInterval(collectionCycleTimer)
    collectionCycleTimer = undefined
    hoveredCollection = null
    cyclingCollectionKey = ''
    cyclingVariantIndex = 0
  }

</script>

<svelte:window onkeydown={handleKeydown} />

<main class="app-shell">
  <header class="toolbar">
    <div class="brand">Findme</div>

    <label class="search-field">
      <Search size={17} />
      <input
        bind:this={searchInput}
        bind:value={query}
        type="search"
        autocomplete="off"
        aria-label="Search assets"
        placeholder="Search assets"
      />
    </label>

    <SearchLaneTabs value={searchMode} onSelect={setSearchMode} />

    <div class="status" class:busy={loading} class:error={error}>
      {#if loading}
        <LoaderCircle size={15} />
      {/if}
      <span>{error || resultLabel}</span>
    </div>
  </header>

  <section class="workspace">
    <section class="main-region" aria-label="Assets">
      <div class="tile-grid" bind:this={tileGridElement}>
        {#each visibleEntries as entry (visibleEntryKey(entry))}
          {#if entry.kind === 'collection'}
            {@const collection = entry.collection}
            {@const displayAsset = collection.representative}
            {@const Icon = assetIcon(displayAsset)}
            <button
              class="asset-tile collection-tile"
              style={`--collection-color: ${collectionColor(collection.collection_key)}`}
              type="button"
              onmouseenter={() => startCollectionCycle(collection)}
              onmouseleave={() => stopCollectionCycle(collection.collection_key)}
              onfocus={() => startCollectionCycle(collection)}
              onblur={() => stopCollectionCycle(collection.collection_key)}
              onclick={() => expandCollection(collection)}
              ondblclick={() => expandCollection(collection)}
            >
              <span class="tile-thumb">
                {#if displayAsset.thumbnail_url}
                  <img src={displayAsset.thumbnail_url} alt="" loading="lazy" />
                {:else}
                  <Icon size={28} />
                {/if}
                <span class="collection-count">{collection.count}</span>
              </span>
              <span class="tile-name">{displayAsset.filename}</span>
              <span class="tile-meta">{displayAsset.directory || displayAsset.source_display_name}</span>
            </button>
          {:else if entry.kind === 'collection-header'}
            {@const collection = entry.collection}
            {@const displayAsset = collection.representative}
            <button
              class="asset-tile collection-header"
              style={`--collection-color: ${collectionColor(collection.collection_key)}`}
              type="button"
              onclick={() => foldCollection(collection.collection_key)}
            >
              <span class="tile-thumb">
                <Layers2 size={32} />
                <span class="collection-count">{collection.count}</span>
              </span>
              <span class="tile-name">{displayAsset.stem}</span>
              <span class="tile-meta">{collection.count} variants</span>
            </button>
          {:else}
            {@const asset = entry.asset}
            {@const Icon = assetIcon(asset)}
            <button
              class:selected={asset.asset_id === selectedAssetId}
              class:collection-member={Boolean(entry.collection_key)}
              class="asset-tile"
              style={entry.collection_key ? `--collection-color: ${collectionColor(entry.collection_key)}` : ''}
              type="button"
              onmouseenter={() => (hoveredAsset = asset)}
              onmouseleave={() => (hoveredAsset = hoveredAsset?.asset_id === asset.asset_id ? null : hoveredAsset)}
              onfocus={() => (hoveredAsset = asset)}
              onblur={() => (hoveredAsset = hoveredAsset?.asset_id === asset.asset_id ? null : hoveredAsset)}
              onclick={() => selectAsset(asset)}
              onauxclick={(event) => handleAssetAuxClick(event, asset)}
            >
              <span class="tile-thumb">
                {#if asset.thumbnail_url}
                  <img src={asset.thumbnail_url} alt="" loading="lazy" />
                {:else}
                  <Icon size={28} />
                {/if}
              </span>
              <span class="tile-name">{asset.filename}</span>
              <span class="tile-meta">{asset.directory || asset.source_display_name}</span>
            </button>
          {/if}
        {:else}
          <div class="empty">No assets found</div>
        {/each}
        {#if cards.length > 0}
          <div class="load-more" bind:this={loadMoreTrigger}>
            {#if hasMoreAssets}
              <button type="button" onclick={loadMoreAssets} disabled={loadingMore}>
                {loadingMore ? 'Loading' : 'Load more'}
              </button>
            {:else}
              <span>End of results</span>
            {/if}
          </div>
        {/if}
      </div>

      {#if hoverPreviewAsset?.thumbnail_url}
        <aside class="hover-preview" aria-label="Hovered asset preview">
          <img src={hoverPreviewAsset.thumbnail_url} alt="" />
          <span>{hoverPreviewAsset.filename}</span>
        </aside>
      {/if}

      {#if selected}
        {@const SelectedIcon = assetIcon(selected.asset)}
        <aside class="asset-panel" aria-label="Selected asset">
          <div class="preview">
            {#if selected.asset.media_type === 'image'}
              <img src={selected.actions.open_url} alt={selected.asset.filename} />
            {:else if selected.asset.media_type === 'video'}
              <!-- svelte-ignore a11y_media_has_caption -->
              <video src={selected.actions.open_url} controls preload="metadata" autoplay muted loop playsinline></video>
            {:else}
              <div class="file-preview">
                <SelectedIcon size={30} />
                <span>{selected.asset.extension.slice(1).toUpperCase()}</span>
              </div>
            {/if}
          </div>

          <div class="asset-title">
            <strong>{selected.asset.filename}</strong>
            <span>{selected.asset.relative_path}</span>
          </div>

          <div class="commands">
            <button type="button" class="copy-button" onclick={copyPrimary} title="Copy primary path">
              <Copy size={16} />
              <span>{copied === selected.asset.asset_id ? 'Copied' : 'Copy'}</span>
            </button>
            <a href={selected.actions.open_url} target="_blank" rel="noreferrer" title="Open original">
              <ExternalLink size={16} />
              <span>Open</span>
            </a>
            <a href={selected.actions.download_url} title="Download original">
              <Download size={16} />
              <span>Download</span>
            </a>
          </div>
        </aside>
      {/if}
    </section>

    <aside class="folder-panel" aria-label="Folder tree" bind:this={folderPanelElement}>
      <div class="panel-title">Folders</div>
      <button
        class="folder-row"
        type="button"
        disabled={filterRules.size === 0}
        onclick={clearFilterRules}
      >
        <Folder size={15} />
        <span>Clear rules</span>
      </button>
      {#each visibleDirectories as directory (directoryKey(directory))}
        {@const children = hasChildren(directory)}
        {@const ruleState = directoryRuleState(directory)}
        <div
          class:path-highlight={hoveredAssetAncestorKeys.has(directoryKey(directory))}
          class:direct-highlight={directHoveredDirectoryKey === directoryKey(directory)}
          class:whitelist={ruleState === 'whitelist'}
          class:blacklist={ruleState === 'blacklist'}
          class:source-root={directory.depth === 0}
          class:selected-folder={selectedDirectoryKey === directoryKey(directory)}
          class:collapsed={children && isCollapsed(directory)}
          class:leaf={!children}
          class="folder-row"
          role="group"
          aria-label={`${directory.name} folder`}
          data-directory-key={directoryDomKey(directory)}
          style={`--depth: ${directory.depth}`}
          onmouseenter={() => setQualityHover(directory)}
          onmouseleave={() => clearQualityHover(directory)}
        >
          <button
            class:source-root={directory.depth === 0}
            class="folder-main"
            type="button"
            onclick={(event) => handleDirectoryClick(event, directory)}
          >
            {#if children}
              <ChevronRight class="folder-chevron" size={14} />
            {:else}
              <span class="folder-spacer"></span>
            {/if}
            {#if directory.depth > 0}
              <Folder size={15} />
            {/if}
            <span>{directory.name}</span>
          </button>
          <em>
            {#if directory.matched_asset_count !== directory.asset_count}
              <b>{directory.matched_asset_count}</b>/{directory.asset_count}
            {:else}
              {directory.asset_count}
            {/if}
          </em>
          <span
            class="folder-control"
            role="group"
            tabindex="-1"
            aria-label={`Current folder controls for ${directory.name}`}
          >
            <button
              class:blocked={ruleState === 'blacklist'}
              class:focused={ruleState === 'whitelist'}
              class:override={directory.quality_override !== null}
              class:has-descendant-settings={directoriesWithDescendantFolderSettings.has(directoryKey(directory))}
              type="button"
              aria-label={folderControlLabel(directory, ruleState)}
              aria-expanded={qualityTargetKey === directoryKey(directory)}
              title={directoriesWithDescendantFolderSettings.has(directoryKey(directory))
                ? 'Contains custom folder settings'
                : undefined}
              onfocus={() => setQualityHover(directory)}
              onclick={(event) => toggleQualityPicker(event, directory)}
            >
              {#if ruleState === 'whitelist'}
                <Check size={13} />
              {:else if ruleState === 'blacklist'}
                <Ban size={13} />
              {/if}
              <span>{directory.quality}</span>
            </button>
            {#if qualityTargetKey === directoryKey(directory)}
              <span class="folder-control-popover" role="group" aria-label={`Folder controls for ${directory.name}`}>
                <span class="scope-actions">
                  <button
                    class:active={ruleState === 'whitelist'}
                    type="button"
                    aria-label={`Focus ${directory.name}`}
                    onclick={(event) => setDirectoryRule(event, directory, 'whitelist')}
                  >
                    <Check size={13} />
                    <span>Focus</span>
                  </button>
                  <button
                    class:active={ruleState === 'blacklist'}
                    type="button"
                    aria-label={`Block ${directory.name}`}
                    onclick={(event) => setDirectoryRule(event, directory, 'blacklist')}
                  >
                    <Ban size={13} />
                    <span>Block</span>
                  </button>
                </span>
                <span class="quality-popover" role="listbox" aria-label={`Set quality for ${directory.name}`}>
                  {#each Array.from({ length: 11 }, (_, quality) => quality) as quality}
                    <button
                      class:active={quality === directory.quality}
                      type="button"
                      role="option"
                      aria-selected={quality === directory.quality}
                      onclick={(event) => changeFolderQuality(event, directory, quality)}
                    >
                      {quality}
                    </button>
                  {/each}
                </span>
              </span>
            {/if}
          </span>
        </div>
        {#if selected && selectedDirectoryKey === directoryKey(directory)}
          {@const SelectedPathIcon = assetIcon(selected.asset)}
          <div
            class="selected-path-row"
            role="note"
            aria-label="Current selection path"
            style={`--depth: ${directory.depth + 2}`}
          >
            <SelectedPathIcon size={13} />
            <span>{selected.asset.filename}</span>
          </div>
        {/if}
      {/each}
    </aside>
  </section>
</main>
