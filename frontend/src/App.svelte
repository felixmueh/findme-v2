<script lang="ts">
  import {
    Copy,
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

  type Source = {
    id: string
    display_name: string
    priority: number
    root: string
  }

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
  let sourceId = $state('')
  let selectedDirectory = $state('')
  let selectedDirectorySourceId = $state('')
  let sources = $state<Source[]>([])
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
  let requestId = 0
  let searchRevision = 0
  let collectionCycleTimer: number | undefined
  let assetRequestController: AbortController | undefined
  let searchInput: HTMLInputElement
  let tileGridElement = $state<HTMLDivElement>()
  let loadMoreTrigger = $state<HTMLDivElement>()

  const pageSize = 100
  const searchSessionId =
    typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random()}`

  const selectedAssetId = $derived(selected?.asset.asset_id ?? '')
  const visibleEntries = $derived.by(() => visibleEntriesForCards(cards, expandedCollections))
  const selectedIndex = $derived(
    visibleEntries.findIndex((entry) => entry.kind === 'asset' && entry.asset.asset_id === selectedAssetId)
  )
  const hoverPreviewAsset = $derived(
    hoveredCollection ? collectionDisplayAsset(hoveredCollection) : hoveredAsset
  )
  const resultLabel = $derived(`${resultTotal} ${resultTotal === 1 ? 'card' : 'cards'}`)
  const visibleDirectories = $derived(directories.filter((directory) => isDirectoryVisible(directory)))
  const visibleDirectoryKeys = $derived.by(
    () => new Set(visibleDirectories.map((directory) => directoryKey(directory)))
  )
  const qualityTargetDirectory = $derived.by(
    () =>
      directories.find(
        (directory) => directoryKey(directory) === (qualityHoverKey || qualityTargetKey)
      ) ?? null
  )
  const directoryPathsWithChildren = $derived.by(() => {
    const paths = new Set<string>()
    for (const directory of directories) {
      if (!directory.path) continue
      const parentPath = directory.path.includes('/')
        ? directory.path.slice(0, directory.path.lastIndexOf('/'))
        : ''
      paths.add(directoryKey({ source_id: directory.source_id, path: parentPath }))
    }
    return paths
  })

  $effect(() => {
    void loadSources()
  })

  $effect(() => {
    query
    searchMode
    sourceId
    selectedDirectory
    selectedDirectorySourceId
    const revision = ++searchRevision
    window.clearTimeout(debounceTimer)
    debounceTimer = window.setTimeout(() => {
      void loadAssets(revision)
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

  async function loadSources() {
    const response = await fetch('/api/sources')
    if (!response.ok) {
      error = 'Could not load sources'
      return
    }
    const body = await response.json()
    sources = body.sources
  }

  function changeSource(event: Event) {
    sourceId = (event.currentTarget as HTMLSelectElement).value
    selectedDirectory = ''
    selectedDirectorySourceId = ''
    collapsedDirectories = new Set()
  }

  function setSearchMode(mode: SearchMode) {
    searchMode = mode
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
    const effectiveSourceId = sourceId || selectedDirectorySourceId
    if (effectiveSourceId) params.set('source_id', effectiveSourceId)
    if (selectedDirectory) params.set('directory_prefix', selectedDirectory)
    const trimmed = query.trim()
    const includeDirectoryCounts = !append && offset === 0
    const path = includeDirectoryCounts ? '/api/search-with-directory-counts' : '/api/search'
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
      if (includeDirectoryCounts && Array.isArray(body.directories)) {
        directories = body.directories
      }
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
    selected = await response.json()
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

  function toggleDirectory(directory: DirectoryNode) {
    if (isDirectorySelected(directory)) {
      selectedDirectory = ''
      selectedDirectorySourceId = ''
      return
    }
    selectedDirectory = directory.path
    selectedDirectorySourceId = sourceId ? '' : directory.source_id
  }

  function directoryKey(directory: Pick<DirectoryNode, 'source_id' | 'path'>) {
    return `${directory.source_id}\u0000${directory.path}`
  }

  function hasChildren(directory: DirectoryNode) {
    return directoryPathsWithChildren.has(directoryKey(directory))
  }

  function isCollapsed(directory: DirectoryNode) {
    return collapsedDirectories.has(directoryKey(directory))
  }

  function isDirectorySelected(directory: DirectoryNode) {
    if (!selectedDirectory && !selectedDirectorySourceId) return false
    return directory.path === selectedDirectory && (sourceId || selectedDirectorySourceId) === directory.source_id
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
    if (event.shiftKey) {
      toggleDirectory(directory)
      return
    }
    if (!hasChildren(directory)) {
      toggleDirectory(directory)
      return
    }
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
    const key = directoryKey(directory)
    qualityTargetKey = qualityTargetKey === key ? '' : key
  }

  function clearDirectory() {
    selectedDirectory = ''
    selectedDirectorySourceId = ''
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

    <select value={sourceId} aria-label="Source" onchange={changeSource}>
      <option value="">All sources</option>
      {#each sources as source}
        <option value={source.id}>{source.display_name}</option>
      {/each}
    </select>

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
                <span class="hover-actions">
                  <span>Expand</span>
                </span>
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
                <span class="hover-actions">
                  <span>Collapse</span>
                </span>
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
                <span class="hover-actions">
                  <span>Preview</span>
                </span>
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

    <aside class="folder-panel" aria-label="Folder tree">
      <div class="panel-title">Folders</div>
      <button
        class:active={selectedDirectory === '' && selectedDirectorySourceId === ''}
        class="folder-row"
        type="button"
        onclick={clearDirectory}
      >
        <Folder size={15} />
        <span>All folders</span>
      </button>
      {#if selectedDirectory && !visibleDirectoryKeys.has(`${selectedDirectorySourceId || sourceId}\u0000${selectedDirectory}`)}
        <button class="folder-row active retained" type="button" onclick={clearDirectory}>
          <span class="folder-spacer"></span>
          <Folder size={15} />
          <span>{selectedDirectory}</span>
          <em>0</em>
        </button>
      {/if}
      {#each visibleDirectories as directory (directoryKey(directory))}
        {@const children = hasChildren(directory)}
        <div
          class:active={isDirectorySelected(directory)}
          class:collapsed={children && isCollapsed(directory)}
          class:leaf={!children}
          class="folder-row"
          role="group"
          aria-label={`${directory.name} folder`}
          style={`--depth: ${directory.depth}`}
          onmouseenter={() => setQualityHover(directory)}
          onmouseleave={() => clearQualityHover(directory)}
        >
          <button class="folder-main" type="button" onclick={(event) => handleDirectoryClick(event, directory)}>
            {#if children}
              <ChevronRight class="folder-chevron" size={14} />
            {:else}
              <span class="folder-spacer"></span>
            {/if}
            <Folder size={15} />
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
            class="quality-control"
            role="group"
            tabindex="-1"
            aria-label={`Quality controls for ${directory.name}`}
          >
            <button
              class:override={directory.quality_override !== null}
              type="button"
              aria-label={`Quality ${directory.quality} for ${directory.name}`}
              aria-expanded={qualityTargetKey === directoryKey(directory)}
              onfocus={() => setQualityHover(directory)}
              onclick={(event) => toggleQualityPicker(event, directory)}
            >
              {directory.quality}
            </button>
            {#if qualityTargetKey === directoryKey(directory)}
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
            {/if}
          </span>
        </div>
      {/each}
    </aside>
  </section>
</main>
