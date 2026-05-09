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
    LoaderCircle,
    Search,
  } from 'lucide-svelte'

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
  }

  type Asset = {
    asset_id: string
    source_id: string
    source_display_name: string
    relative_path: string
    filename: string
    stem: string
    extension: string
    directory: string
    size_bytes: number
    media_type: string
    thumbnail_url?: string
    thumbnail_width?: number
    thumbnail_height?: number
    score?: number
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
  let sourceId = $state('')
  let selectedDirectory = $state('')
  let selectedDirectorySourceId = $state('')
  let sources = $state<Source[]>([])
  let directories = $state<DirectoryNode[]>([])
  let collapsedDirectories = $state<Set<string>>(new Set())
  let assets = $state<Asset[]>([])
  let selected = $state<AssetDetail | null>(null)
  let hoveredAsset = $state<Asset | null>(null)
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
  let assetRequestController: AbortController | undefined
  let directoryRequestController: AbortController | undefined
  let searchInput: HTMLInputElement
  let tileGridElement = $state<HTMLDivElement>()
  let loadMoreTrigger = $state<HTMLDivElement>()

  const pageSize = 100
  const searchSessionId =
    typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random()}`
  const directorySearchSessionId = `${searchSessionId}:directories`

  const selectedAssetId = $derived(selected?.asset.asset_id ?? '')
  const selectedIndex = $derived(assets.findIndex((asset) => asset.asset_id === selectedAssetId))
  const resultLabel = $derived(`${resultTotal} ${resultTotal === 1 ? 'asset' : 'assets'}`)
  const visibleDirectories = $derived(directories.filter((directory) => isDirectoryVisible(directory)))
  const visibleDirectoryKeys = $derived.by(
    () => new Set(visibleDirectories.map((directory) => directoryKey(directory)))
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
    void loadDirectories()
  })

  $effect(() => {
    query
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
    query
    sourceId
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

  async function loadSources() {
    const response = await fetch('/api/sources')
    if (!response.ok) {
      error = 'Could not load sources'
      return
    }
    const body = await response.json()
    sources = body.sources
    await loadDirectories()
  }

  function changeSource(event: Event) {
    sourceId = (event.currentTarget as HTMLSelectElement).value
    selectedDirectory = ''
    selectedDirectorySourceId = ''
    collapsedDirectories = new Set()
  }

  async function loadDirectories(revision?: number) {
    directoryRequestController?.abort()
    const controller = new AbortController()
    directoryRequestController = controller
    const params = new URLSearchParams()
    if (sourceId) params.set('source_id', sourceId)
    const trimmed = query.trim()
    if (trimmed) {
      params.set('q', trimmed)
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
        directories = body.directories
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
    await loadAssetPage({ offset: assets.length, append: true, revision: searchRevision })
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
    const path = trimmed ? '/api/search' : '/api/assets'
    if (trimmed) {
      params.set('q', trimmed)
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
      const nextAssets = body.assets as Asset[]
      resultTotal = Number(body.total ?? nextAssets.length)
      assets = append ? [...assets, ...nextAssets] : nextAssets
      hasMoreAssets = assets.length < resultTotal
      if (!append && assets.length === 0) {
        selected = null
      } else if (!append && selected && !assets.some((asset) => asset.asset_id === selectedAssetId)) {
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
    if (assets.length === 0) return
    const fallback = selectedIndex < 0 ? 0 : selectedIndex
    const next = Math.min(Math.max(fallback + offset, 0), assets.length - 1)
    void selectAsset(assets[next])
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

  async function selectFirstResultFromSearch() {
    window.clearTimeout(debounceTimer)
    const revision = ++searchRevision
    await loadAssetPage({ offset: 0, append: false, revision })
    if (assets.length === 0) return
    await selectAsset(assets[0])
    searchInput?.blur()
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
      void selectFirstResultFromSearch()
      return
    }
    if (event.key === 'Escape') {
      event.preventDefault()
      selected = null
      return
    }
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
</script>

<svelte:window onkeydown={handleKeydown} />

<main class="app-shell">
  <header class="toolbar">
    <div class="brand">Findme</div>

    <label class="search-field">
      <Search size={17} />
      <input bind:this={searchInput} bind:value={query} type="search" autocomplete="off" placeholder="Search assets" />
    </label>

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
        {#each assets as asset (asset.asset_id)}
          {@const Icon = assetIcon(asset)}
          <button
            class:selected={asset.asset_id === selectedAssetId}
            class="asset-tile"
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
        {:else}
          <div class="empty">No assets found</div>
        {/each}
        {#if assets.length > 0}
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

      {#if hoveredAsset?.thumbnail_url}
        <aside class="hover-preview" aria-label="Hovered asset preview">
          <img src={hoveredAsset.thumbnail_url} alt="" />
          <span>{hoveredAsset.filename}</span>
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
              <video src={selected.actions.open_url} controls preload="metadata"></video>
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
        <button
          class:active={isDirectorySelected(directory)}
          class:collapsed={children && isCollapsed(directory)}
          class:leaf={!children}
          class="folder-row"
          style={`--depth: ${directory.depth}`}
          type="button"
          onclick={(event) => handleDirectoryClick(event, directory)}
        >
          {#if children}
            <ChevronRight class="folder-chevron" size={14} />
          {:else}
            <span class="folder-spacer"></span>
          {/if}
          <Folder size={15} />
          <span>{directory.name}</span>
          <em>
            {#if directory.matched_asset_count !== directory.asset_count}
              <b>{directory.matched_asset_count}</b>/{directory.asset_count}
            {:else}
              {directory.asset_count}
            {/if}
          </em>
        </button>
      {/each}
    </aside>
  </section>
</main>
