<script lang="ts">
  type Source = {
    id: string
    display_name: string
    priority: number
    root: string
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
  let sources = $state<Source[]>([])
  let assets = $state<Asset[]>([])
  let selected = $state<AssetDetail | null>(null)
  let loading = $state(false)
  let error = $state('')
  let copied = $state('')
  let debounceTimer: number | undefined
  let requestId = 0

  const selectedAssetId = $derived(selected?.asset.asset_id ?? '')

  $effect(() => {
    void loadSources()
    void loadAssets()
  })

  $effect(() => {
    query
    sourceId
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
  }

  async function loadAssets() {
    const currentRequest = ++requestId
    const params = new URLSearchParams()
    params.set('limit', '100')
    if (sourceId) params.set('source_id', sourceId)
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

  function fileUrl(asset: Asset) {
    return `/api/assets/${asset.asset_id}/file`
  }

  function formatBytes(bytes: number) {
    if (bytes < 1024) return `${bytes} B`
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  }
</script>

<main class="app-shell">
  <header class="topbar">
    <div>
      <h1>Findme</h1>
      <p>{assets.length} results</p>
    </div>
    <div class="search-controls">
      <input
        bind:value={query}
        class="search-input"
        type="search"
        autocomplete="off"
        placeholder="Search assets"
      />
      <select bind:value={sourceId} aria-label="Source">
        <option value="">All sources</option>
        {#each sources as source}
          <option value={source.id}>{source.display_name}</option>
        {/each}
      </select>
    </div>
  </header>

  {#if error}
    <div class="status error">{error}</div>
  {:else if loading}
    <div class="status">Loading</div>
  {/if}

  <section class="workspace">
    <aside class="result-pane" aria-label="Results">
      {#each assets as asset}
        <button
          class:active={asset.asset_id === selectedAssetId}
          class="result-row"
          type="button"
          onclick={() => selectAsset(asset)}
        >
          <span class="thumb">
            {#if asset.thumbnail_url}
              <img src={asset.thumbnail_url} alt="" loading="lazy" />
            {:else}
              <span class="media-badge">{asset.extension.slice(1).toUpperCase()}</span>
            {/if}
          </span>
          <span class="result-text">
            <strong>{asset.filename}</strong>
            <small>{asset.directory || asset.source_display_name}</small>
          </span>
        </button>
      {:else}
        <div class="empty">No assets found</div>
      {/each}
    </aside>

    <section class="detail-pane" aria-label="Selected asset">
      {#if selected}
        <div class="preview">
          {#if selected.asset.media_type === 'image'}
            <img src={selected.actions.open_url} alt={selected.asset.filename} />
          {:else if selected.asset.media_type === 'video'}
            <!-- svelte-ignore a11y_media_has_caption -->
            <video src={selected.actions.open_url} controls preload="metadata"></video>
          {:else}
            <div class="file-preview">{selected.asset.extension.slice(1).toUpperCase()}</div>
          {/if}
        </div>

        <div class="asset-meta">
          <div>
            <h2>{selected.asset.filename}</h2>
            <p>{selected.asset.relative_path}</p>
          </div>
          <div class="commands">
            <button type="button" onclick={copyPrimary}>
              {copied === selected.asset.asset_id ? 'Copied' : 'Copy'}
            </button>
            <a href={selected.actions.open_url} target="_blank" rel="noreferrer">Open</a>
            <a href={selected.actions.download_url}>Download</a>
          </div>
        </div>

        <dl class="details">
          <div>
            <dt>Primary</dt>
            <dd>{selected.actions.primary.value}</dd>
          </div>
          <div>
            <dt>Source</dt>
            <dd>{selected.asset.source_display_name}</dd>
          </div>
          <div>
            <dt>Size</dt>
            <dd>{formatBytes(selected.asset.size_bytes)}</dd>
          </div>
          <div>
            <dt>Type</dt>
            <dd>{selected.asset.media_type}</dd>
          </div>
        </dl>
      {:else}
        <div class="empty detail-empty">No asset selected</div>
      {/if}
    </section>
  </section>
</main>
