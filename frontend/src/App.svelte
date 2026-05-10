<script lang="ts">
  import { tick, untrack } from 'svelte'
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
    Plus,
    RotateCcw,
    Save,
    Trash2,
    Volume2,
    VolumeX,
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
    included_asset_count: number
    quality: number
    quality_override: number | null
    selection_state: FilterRuleState | null
  }

  type FilterRuleState = 'whitelist' | 'blacklist'

  type SearchProfile = {
    id: string
    name: string
    kind: 'none' | 'default' | 'custom'
    version: number
    sort_order: number
  }

  type SearchProfileSession = {
    search_session: string
    active_profile_id: string | null
    active_profile_name: string | null
    active_profile_kind: 'none' | 'default' | 'custom' | null
    dirty_scoring: boolean
    dirty_selection: boolean
    dirty: boolean
  }

  type UrlAction = {
    id: string
    label: string
    url: string
  }

  type ClipboardUrlAction = {
    id: string
    label: string
    enabled: boolean
    url: string | null
  }

  type ValueAction = {
    id: string
    label: string
    value: string
  }

  type AssetDetail = {
    asset: Asset
    actions: {
      open: UrlAction
      copy: ClipboardUrlAction
      copy_thumbnail: ClipboardUrlAction
      download: UrlAction
      copy_path: ValueAction
      copy_foundryvtt_path: ValueAction
    }
  }

  let query = $state('')
  let searchMode = $state<SearchMode>('strict')
  let profiles = $state<SearchProfile[]>([])
  let profileSession = $state<SearchProfileSession | null>(null)
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
  let errorTimer: number | undefined
  let requestId = 0
  let searchRevision = 0
  let collectionCycleTimer: number | undefined
  let assetRequestController: AbortController | undefined
  let hoverAudioElement = $state<HTMLAudioElement>()
  let hoverPreviewMuted = $state(true)
  let hoverPreviewVolume = $state(0.5)
  let hoverClearTimer: number | undefined
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
  const hoverAudioUrl = $derived(
    hoverPreviewAsset?.media_type === 'audio' && hoverPreviewAsset.asset_id !== selectedAssetId
      ? assetFileUrl(hoverPreviewAsset)
      : ''
  )
  const resultLabel = $derived(`${resultTotal} ${resultTotal === 1 ? 'card' : 'cards'}`)
  const visibleDirectories = $derived(directories.filter((directory) => isDirectoryVisible(directory)))
  const filterRules = $derived.by(
    () =>
      new Map(
        directories
          .filter((directory) => directory.selection_state !== null)
          .map((directory) => [directoryKey(directory), directory.selection_state as FilterRuleState])
      )
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

  void loadSearchProfiles()

  $effect(() => {
    query
    searchMode
    profileSession?.active_profile_id
    profileSession?.dirty
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

  $effect(() => {
    const audio = hoverAudioElement
    const url = hoverAudioUrl
    if (!audio || !url) return

    audio.muted = untrack(() => hoverPreviewMuted)
    audio.volume = untrack(() => hoverPreviewVolume)
    audio.src = url
    audio.load()
    const playback = audio.play()
    if (playback !== undefined) {
      playback.catch(() => {
        // Browsers can block audible hover playback until the page has user activation.
      })
    }
    return () => stopAudioPreview(audio)
  })

  $effect(() => {
    const audio = hoverAudioElement
    const muted = hoverPreviewMuted
    const volume = hoverPreviewVolume
    if (!audio) return
    audio.muted = muted
    audio.volume = volume
  })

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
    params.set('search_session', searchSessionId)
    const trimmed = query.trim()
    const includeDirectoryCounts = !append && offset === 0
    const path = includeDirectoryCounts ? '/api/search-with-directory-counts' : '/api/search'
    if (trimmed) {
      params.set('q', trimmed)
      params.set('mode', searchMode)
      params.set('search_revision', String(revision))
    }

    if (append) {
      loadingMore = true
    } else {
      loading = true
      hasMoreAssets = false
      resultTotal = 0
    }
    clearError()
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
        const nextDirectories = body.directories as DirectoryNode[]
        directories = nextDirectories
        if (!hasInitializedFolderCollapse) {
          collapsedDirectories = initiallyCollapsedDirectoryKeys(nextDirectories)
          hasInitializedFolderCollapse = true
        }
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
      if (!isAbortError(caught) && currentRequest === requestId) showError('Search failed')
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
      showError('Could not load asset')
      return
    }
    const detail = (await response.json()) as AssetDetail
    selected = detail
    revealSelectedAssetDirectory(detail.asset)
  }

  async function copyFoundryVttPath() {
    if (!selected) return
    await copyValueAction(selected.actions.copy_foundryvtt_path)
  }

  async function copyValueAction(action: ValueAction) {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(action.value)
      } else if (!legacyCopyText(action.value)) {
        throw new Error('legacy copy failed')
      }
      markCopied(action.id)
    } catch {
      if (legacyCopyText(action.value)) {
        markCopied(action.id)
      } else {
        showError('Text clipboard is not available', { transient: true })
      }
    }
  }

  async function copyUrlAction(action: ClipboardUrlAction) {
    if (!action.enabled || !action.url) return
    if (!navigator.clipboard?.write || typeof ClipboardItem === 'undefined') {
      showError('Image clipboard requires HTTPS or localhost', { transient: true })
      return
    }
    try {
      const response = await fetch(action.url)
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      const blob = await response.blob()
      const type = blob.type || 'image/png'
      await navigator.clipboard.write([new ClipboardItem({ [type]: blob })])
      markCopied(action.id)
    } catch {
      showError('Could not copy image', { transient: true })
    }
  }

  function legacyCopyText(value: string) {
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const textarea = document.createElement('textarea')
    textarea.value = value
    textarea.setAttribute('readonly', '')
    textarea.style.position = 'fixed'
    textarea.style.top = '0'
    textarea.style.left = '0'
    textarea.style.width = '1px'
    textarea.style.height = '1px'
    textarea.style.opacity = '0'
    document.body.append(textarea)
    textarea.focus()
    textarea.select()
    let copiedText = false
    try {
      copiedText = document.execCommand('copy')
    } finally {
      textarea.remove()
      previousFocus?.focus()
    }
    return copiedText
  }

  function markCopied(actionId: string) {
    clearError()
    copied = actionId
    window.setTimeout(() => {
      if (copied === actionId) copied = ''
    }, 1200)
  }

  function showError(message: string, options: { transient?: boolean } = {}) {
    window.clearTimeout(errorTimer)
    errorTimer = undefined
    error = message
    if (options.transient) {
      errorTimer = window.setTimeout(() => {
        if (error === message) error = ''
        errorTimer = undefined
      }, 2800)
    }
  }

  function clearError() {
    window.clearTimeout(errorTimer)
    errorTimer = undefined
    error = ''
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
      void copyFoundryVttPath()
    }
  }

  function assetIcon(asset: Asset) {
    if (asset.media_type === 'image') return ImageIcon
    if (asset.media_type === 'video') return Film
    if (asset.media_type === 'audio') return Volume2
    return FileQuestion
  }

  function setHoveredAsset(asset: Asset) {
    window.clearTimeout(hoverClearTimer)
    hoverClearTimer = undefined
    hoveredAsset = asset
  }

  function scheduleClearHoveredAsset(asset: Asset) {
    window.clearTimeout(hoverClearTimer)
    hoverClearTimer = window.setTimeout(() => {
      if (hoveredAsset?.asset_id === asset.asset_id) hoveredAsset = null
      hoverClearTimer = undefined
    }, 120)
  }

  function keepHoverPreviewOpen() {
    window.clearTimeout(hoverClearTimer)
    hoverClearTimer = undefined
  }

  function clearHoverPreview() {
    window.clearTimeout(hoverClearTimer)
    hoverClearTimer = undefined
    hoveredAsset = null
  }

  function clearHoveredAssetOnCardFocusOut(event: FocusEvent, asset: Asset) {
    const nextTarget = event.relatedTarget
    if (nextTarget instanceof Node && event.currentTarget instanceof HTMLElement) {
      if (event.currentTarget.contains(nextTarget)) return
    }
    if (hoveredAsset?.asset_id === asset.asset_id) clearHoverPreview()
  }

  function setHoverPreviewVolume(event: Event) {
    const input = event.currentTarget as HTMLInputElement
    const nextVolume = Number(input.value) / 100
    hoverPreviewVolume = Math.max(0, Math.min(1, nextVolume))
  }

  function toggleHoverPreviewMuted() {
    hoverPreviewMuted = !hoverPreviewMuted
  }

  function selectedPreviewStyle(asset: Pick<Asset, 'media_type' | 'thumbnail_width' | 'thumbnail_height'>) {
    if (
      (asset.media_type === 'image' || asset.media_type === 'video') &&
      asset.thumbnail_width &&
      asset.thumbnail_height
    ) {
      return `--selected-preview-aspect: ${asset.thumbnail_width} / ${asset.thumbnail_height}`
    }
    return ''
  }

  function assetFileUrl(asset: Pick<Asset, 'asset_id'>) {
    return `/api/assets/${asset.asset_id}/file`
  }

  function stopAudioPreview(audio: HTMLAudioElement) {
    audio.pause()
    try {
      audio.currentTime = 0
    } catch {
      // Some browsers reject seeking before metadata is available.
    }
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

  function directoryRuleState(directory: DirectoryNode) {
    return directory.selection_state
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

  async function toggleDirectoryRule(directory: DirectoryNode, state: FilterRuleState) {
    const nextState = directory.selection_state === state ? null : state
    await updateFolderSetting(directory, { selection_state: nextState })
  }

  function setDirectoryRule(event: MouseEvent, directory: DirectoryNode, state: FilterRuleState) {
    event.stopPropagation()
    void toggleDirectoryRule(directory, state)
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
      void toggleDirectoryRule(directory, event.shiftKey ? 'whitelist' : 'blacklist')
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

  async function updateFolderSetting(
    directory: DirectoryNode,
    patch: { quality_override?: number | null; selection_state?: FilterRuleState | null }
  ) {
    const response = await fetch('/api/search-profile/settings', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        search_session: searchSessionId,
        source_id: directory.source_id,
        directory: directory.path,
        ...patch,
      }),
    })
    if (!response.ok) {
      showError('Could not update profile')
      return
    }
    const body = (await response.json()) as { session: SearchProfileSession }
    profileSession = body.session
    const revision = ++searchRevision
    await loadAssetPage({ offset: 0, append: false, revision })
  }

  async function setFolderQuality(directory: DirectoryNode, quality: number) {
    const nextQuality = Math.max(0, Math.min(10, quality))
    if (nextQuality === directory.quality && directory.quality_override === null) return
    await updateFolderSetting(directory, {
      quality_override: nextQuality === 5 ? null : nextQuality,
    })
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
      void toggleDirectoryRule(directory, event.shiftKey ? 'whitelist' : 'blacklist')
      qualityTargetKey = ''
      return
    }
    const key = directoryKey(directory)
    qualityTargetKey = qualityTargetKey === key ? '' : key
  }

  function openAsset(asset: Asset) {
    window.open(assetFileUrl(asset), '_blank', 'noopener')
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

  async function loadSearchProfiles() {
    const response = await fetch(`/api/search-profiles?search_session=${encodeURIComponent(searchSessionId)}`)
    if (!response.ok) {
      showError('Could not load profiles')
      return
    }
    const body = (await response.json()) as { profiles: SearchProfile[]; session: SearchProfileSession }
    profiles = body.profiles
    profileSession = body.session
  }

  async function selectProfile(event: Event) {
    const profileId = (event.currentTarget as HTMLSelectElement).value
    const response = await fetch('/api/search-profiles/select', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ search_session: searchSessionId, profile_id: profileId }),
    })
    if (response.status === 409 && window.confirm('Discard unsaved profile changes?')) {
      await selectProfileById(profileId, true)
      return
    }
    if (!response.ok) {
      showError('Could not select profile')
      return
    }
    const body = (await response.json()) as { session: SearchProfileSession }
    profileSession = body.session
    const revision = ++searchRevision
    await loadAssetPage({ offset: 0, append: false, revision })
  }

  async function selectProfileById(profileId: string, discard = false) {
    const response = await fetch('/api/search-profiles/select', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ search_session: searchSessionId, profile_id: profileId, discard }),
    })
    if (!response.ok) {
      showError('Could not select profile')
      return
    }
    const body = (await response.json()) as { session: SearchProfileSession }
    profileSession = body.session
    const revision = ++searchRevision
    await loadAssetPage({ offset: 0, append: false, revision })
  }

  async function saveProfile() {
    const name = profileSession?.active_profile_id ? undefined : window.prompt('Profile name')?.trim()
    if (profileSession?.active_profile_id === null && !name) return
    const response = await fetch('/api/search-profile/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ search_session: searchSessionId, name }),
    })
    if (!response.ok) {
      showError(response.status === 409 ? 'Profile changed elsewhere' : 'Could not save profile')
      return
    }
    await loadSearchProfiles()
    const revision = ++searchRevision
    await loadAssetPage({ offset: 0, append: false, revision })
  }

  async function createProfile() {
    const name = window.prompt('Profile name')?.trim()
    if (!name) return
    const response = await fetch('/api/search-profiles', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ search_session: searchSessionId, name }),
    })
    if (!response.ok) {
      showError('Could not create profile')
      return
    }
    await loadSearchProfiles()
  }

  async function resetScoring() {
    await profileCommand('/api/search-profile/reset-scoring')
  }

  async function resetSelection() {
    await profileCommand('/api/search-profile/reset-selection')
  }

  async function deleteProfile() {
    if (!profileSession?.active_profile_id || profileSession.active_profile_kind !== 'custom') return
    if (!window.confirm(`Delete ${profileSession.active_profile_name}?`)) return
    const response = await fetch(
      `/api/search-profiles/${encodeURIComponent(profileSession.active_profile_id)}?search_session=${encodeURIComponent(searchSessionId)}`,
      { method: 'DELETE' }
    )
    if (!response.ok) {
      showError('Could not delete profile')
      return
    }
    await loadSearchProfiles()
    const revision = ++searchRevision
    await loadAssetPage({ offset: 0, append: false, revision })
  }

  async function profileCommand(path: string) {
    const response = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ search_session: searchSessionId }),
    })
    if (!response.ok) {
      showError('Could not update profile')
      return
    }
    const body = (await response.json()) as { session: SearchProfileSession }
    profileSession = body.session
    const revision = ++searchRevision
    await loadAssetPage({ offset: 0, append: false, revision })
  }

</script>

<svelte:window onkeydown={handleKeydown} />

{#if hoverAudioUrl}
  <!-- svelte-ignore a11y_media_has_caption -->
  <audio bind:this={hoverAudioElement} preload="none"></audio>
{/if}

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

    <div class="profile-controls" role="group" aria-label="Search profile">
      <select aria-label="Search profile" value={profileSession?.active_profile_id ?? ''} onchange={selectProfile}>
        {#each profiles as profile}
          <option value={profile.id}>{profile.name}</option>
        {/each}
      </select>
      <button type="button" title="Save profile" onclick={saveProfile} disabled={!profileSession?.dirty}>
        <Save size={15} />
      </button>
      <button type="button" title="Create profile" onclick={createProfile}>
        <Plus size={15} />
      </button>
      <button type="button" title="Reset scoring" onclick={resetScoring} disabled={!profileSession?.dirty_scoring}>
        <RotateCcw size={15} />
      </button>
      <button type="button" title="Reset selection" onclick={resetSelection} disabled={!profileSession?.dirty_selection}>
        <Ban size={15} />
      </button>
      <button
        type="button"
        title="Delete profile"
        onclick={deleteProfile}
        disabled={profileSession?.active_profile_kind !== 'custom'}
      >
        <Trash2 size={15} />
      </button>
    </div>

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
            {#if asset.media_type === 'audio'}
              <div
                class:selected={asset.asset_id === selectedAssetId}
                class:collection-member={Boolean(entry.collection_key)}
                class="asset-card audio-asset-card"
                style={entry.collection_key ? `--collection-color: ${collectionColor(entry.collection_key)}` : ''}
                role="group"
                aria-label={`Audio asset card for ${asset.filename}`}
                onmouseenter={() => setHoveredAsset(asset)}
                onmouseleave={() => scheduleClearHoveredAsset(asset)}
                onfocusin={() => setHoveredAsset(asset)}
                onfocusout={(event) => clearHoveredAssetOnCardFocusOut(event, asset)}
              >
                <button
                  class="asset-card-main"
                  type="button"
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
                <div class="card-audio-controls" role="group" aria-label="Audio preview controls">
                  <button
                    type="button"
                    class="preview-mute-button"
                    onclick={toggleHoverPreviewMuted}
                    aria-label={hoverPreviewMuted ? 'Unmute preview' : 'Mute preview'}
                    title={hoverPreviewMuted ? 'Unmute preview' : 'Mute preview'}
                  >
                    {#if hoverPreviewMuted}
                      <VolumeX size={16} />
                    {:else}
                      <Volume2 size={16} />
                    {/if}
                  </button>
                  <label class="preview-volume-control">
                    <input
                      type="range"
                      min="0"
                      max="100"
                      step="1"
                      value={Math.round(hoverPreviewVolume * 100)}
                      oninput={setHoverPreviewVolume}
                      aria-label="Preview volume"
                    />
                  </label>
                </div>
              </div>
            {:else}
              <button
                class:selected={asset.asset_id === selectedAssetId}
                class:collection-member={Boolean(entry.collection_key)}
                class="asset-tile"
                style={entry.collection_key ? `--collection-color: ${collectionColor(entry.collection_key)}` : ''}
                type="button"
                onmouseenter={() => setHoveredAsset(asset)}
                onmouseleave={() => scheduleClearHoveredAsset(asset)}
                onfocus={() => setHoveredAsset(asset)}
                onblur={() => {
                  if (hoveredAsset?.asset_id === asset.asset_id) clearHoverPreview()
                }}
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

      {#if hoverPreviewAsset}
        {@const HoverIcon = assetIcon(hoverPreviewAsset)}
        <aside
          class="hover-preview"
          aria-label="Hovered asset preview"
          onmouseenter={keepHoverPreviewOpen}
          onmouseleave={clearHoverPreview}
        >
          {#if hoverPreviewAsset.thumbnail_url}
            <img src={hoverPreviewAsset.thumbnail_url} alt="" />
          {:else}
            <div class="file-preview">
              <HoverIcon size={30} />
              <span>{hoverPreviewAsset.extension.slice(1).toUpperCase()}</span>
            </div>
          {/if}
          <span class="hover-preview-name">{hoverPreviewAsset.filename}</span>
        </aside>
      {/if}

      {#if selected}
        {@const SelectedIcon = assetIcon(selected.asset)}
        {@const actions = selected.actions}
        <aside class="asset-panel" aria-label="Selected asset">
          <div
            class="preview"
            class:media-preview={selected.asset.media_type === 'image' || selected.asset.media_type === 'video'}
            class:audio-preview-shell={selected.asset.media_type === 'audio'}
            style={selectedPreviewStyle(selected.asset)}
          >
            {#if selected.asset.media_type === 'image'}
              <img src={actions.open.url} alt={selected.asset.filename} />
            {:else if selected.asset.media_type === 'video'}
              <!-- svelte-ignore a11y_media_has_caption -->
              <video src={actions.open.url} controls preload="metadata" autoplay muted loop playsinline></video>
            {:else if selected.asset.media_type === 'audio'}
              <div class="audio-preview">
                <div class="file-preview">
                  <SelectedIcon size={30} />
                  <span>{selected.asset.extension.slice(1).toUpperCase()}</span>
                </div>
                <!-- svelte-ignore a11y_media_has_caption -->
                <audio src={actions.open.url} controls preload="metadata"></audio>
              </div>
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
            <a href={actions.open.url} target="_blank" rel="noreferrer" title="Open original">
              <ExternalLink size={16} />
              <span>Open</span>
            </a>
            <button
              type="button"
              class="copy-button"
              onclick={() => copyUrlAction(actions.copy)}
              disabled={!actions.copy.enabled}
              title="Copy original image"
            >
              <Copy size={16} />
              <span>{copied === actions.copy.id ? 'Copied' : 'Copy'}</span>
            </button>
            <button
              type="button"
              class="copy-button"
              onclick={() => copyUrlAction(actions.copy_thumbnail)}
              disabled={!actions.copy_thumbnail.enabled}
              title="Copy thumbnail image"
            >
              <ImageIcon size={16} />
              <span>{copied === actions.copy_thumbnail.id ? 'Copied' : 'Copy Thumbnail'}</span>
            </button>
            <a href={actions.download.url} title="Download original">
              <Download size={16} />
              <span>Download</span>
            </a>
            <button
              type="button"
              class="copy-button"
              onclick={() => copyValueAction(actions.copy_path)}
              title="Copy absolute path"
            >
              <Copy size={16} />
              <span>{copied === actions.copy_path.id ? 'Copied' : 'Copy path'}</span>
            </button>
            <button
              type="button"
              class="copy-button"
              onclick={() => copyValueAction(actions.copy_foundryvtt_path)}
              title="Copy FoundryVTT path"
            >
              <Copy size={16} />
              <span>{copied === actions.copy_foundryvtt_path.id ? 'Copied' : 'Copy FoundryVTT path'}</span>
            </button>
          </div>
        </aside>
      {/if}
    </section>

    <aside class="folder-panel" aria-label="Folder tree" bind:this={folderPanelElement}>
      <div class="panel-title">Folders</div>
      <button
        class="folder-row"
        type="button"
        disabled={!profileSession?.dirty_selection}
        onclick={resetSelection}
      >
        <Folder size={15} />
        <span>Reset selection</span>
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
