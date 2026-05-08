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
    path: string
    name: string
    depth: number
    asset_count: number
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
  let sources = $state<Source[]>([])
  let directories = $state<DirectoryNode[]>([])
  let collapsedDirectories = $state<Set<string>>(new Set())
  let assets = $state<Asset[]>([])
  let selected = $state<AssetDetail | null>(null)
  let loading = $state(false)
  let error = $state('')
  let copied = $state('')
  let debounceTimer: number | undefined
  let requestId = 0

  const selectedAssetId = $derived(selected?.asset.asset_id ?? '')
  const selectedIndex = $derived(assets.findIndex((asset) => asset.asset_id === selectedAssetId))
  const resultLabel = $derived(`${assets.length} ${assets.length === 1 ? 'asset' : 'assets'}`)
  const visibleDirectories = $derived(directories.filter((directory) => isDirectoryVisible(directory.path)))

  $effect(() => {
    void loadSources()
  })

  $effect(() => {
    sourceId
    selectedDirectory = ''
    void loadDirectories()
  })

  $effect(() => {
    query
    sourceId
    selectedDirectory
    window.clearTimeout(debounceTimer)
    debounceTimer = window.setTimeout(() => {
      void loadAssets()
    }, 150)
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

  async function loadDirectories() {
    const params = new URLSearchParams()
    if (sourceId) params.set('source_id', sourceId)
    const response = await fetch(`/api/directories?${params}`)
    if (!response.ok) {
      error = 'Could not load folders'
      return
    }
    const body = await response.json()
    directories = body.directories
  }

  async function loadAssets() {
    const currentRequest = ++requestId
    const params = new URLSearchParams()
    params.set('limit', '100')
    if (sourceId) params.set('source_id', sourceId)
    if (selectedDirectory) params.set('directory_prefix', selectedDirectory)
    const trimmed = query.trim()
    const path = trimmed ? '/api/search' : '/api/assets'
    if (trimmed) params.set('q', trimmed)

    loading = true
    error = ''
    try {
      const response = await fetch(`${path}?${params}`)
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      const body = await response.json()
      if (currentRequest !== requestId) return
      assets = body.assets
      if (assets.length === 0) {
        selected = null
      } else if (!selected || !assets.some((asset) => asset.asset_id === selectedAssetId)) {
        await selectAsset(assets[0])
      }
    } catch {
      if (currentRequest === requestId) error = 'Search failed'
    } finally {
      if (currentRequest === requestId) loading = false
    }
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

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
      event.preventDefault()
      chooseRelative(1)
    } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
      event.preventDefault()
      chooseRelative(-1)
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

  function toggleDirectory(path: string) {
    selectedDirectory = selectedDirectory === path ? '' : path
  }

  function hasChildren(path: string) {
    const prefix = path ? `${path}/` : ''
    const childDepth = path ? path.split('/').length + 1 : 1
    return directories.some(
      (directory) =>
        (path === '' || directory.path.startsWith(prefix)) &&
        directory.path !== path &&
        directory.depth === childDepth,
    )
  }

  function isCollapsed(path: string) {
    return collapsedDirectories.has(path)
  }

  function isDirectoryVisible(path: string) {
    if (!path) return true
    if (collapsedDirectories.has('')) return false
    const parts = path.split('/')
    for (let index = 1; index < parts.length; index += 1) {
      if (collapsedDirectories.has(parts.slice(0, index).join('/'))) return false
    }
    return true
  }

  function handleDirectoryClick(event: MouseEvent, path: string) {
    if (event.shiftKey) {
      toggleDirectory(path)
      return
    }
    if (!hasChildren(path)) return
    const next = new Set(collapsedDirectories)
    if (next.has(path)) {
      next.delete(path)
    } else {
      next.add(path)
    }
    collapsedDirectories = next
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<main class="app-shell">
  <header class="toolbar">
    <div class="brand">Findme</div>

    <label class="search-field">
      <Search size={17} />
      <input bind:value={query} type="search" autocomplete="off" placeholder="Search assets" />
    </label>

    <select bind:value={sourceId} aria-label="Source">
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
      <div class="tile-grid">
        {#each assets as asset}
          {@const Icon = assetIcon(asset)}
          <button
            class:selected={asset.asset_id === selectedAssetId}
            class="asset-tile"
            type="button"
            onclick={() => selectAsset(asset)}
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
      </div>

      <aside class="asset-panel" aria-label="Selected asset">
        {#if selected}
          {@const SelectedIcon = assetIcon(selected.asset)}
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
        {:else}
          <div class="empty">No asset selected</div>
        {/if}
      </aside>
    </section>

    <aside class="folder-panel" aria-label="Folder tree">
      <div class="panel-title">Folders</div>
      <button
        class:active={selectedDirectory === ''}
        class="folder-row"
        type="button"
        onclick={() => (selectedDirectory = '')}
      >
        <Folder size={15} />
        <span>All folders</span>
      </button>
      {#each visibleDirectories as directory}
        {@const children = hasChildren(directory.path)}
        <button
          class:active={directory.path === selectedDirectory}
          class:collapsed={children && isCollapsed(directory.path)}
          class:leaf={!children}
          class="folder-row"
          style={`--depth: ${directory.depth}`}
          type="button"
          onclick={(event) => handleDirectoryClick(event, directory.path)}
        >
          {#if children}
            <ChevronRight class="folder-chevron" size={14} />
          {:else}
            <span class="folder-spacer"></span>
          {/if}
          <Folder size={15} />
          <span>{directory.name}</span>
          <em>{directory.asset_count}</em>
        </button>
      {/each}
    </aside>
  </section>
</main>
