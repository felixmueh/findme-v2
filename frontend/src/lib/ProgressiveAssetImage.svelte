<script lang="ts">
  import { onMount, tick } from 'svelte'
  import {
    nextProgressiveImageSource,
    thumbnailSourcesForAsset,
    type Asset,
    type ProgressiveImageSource,
  } from './result-cards'

  type Props = {
    asset: Pick<
      Asset,
      | 'media_type'
      | 'thumbnail_url'
      | 'thumbnail_width'
      | 'thumbnail_height'
      | 'thumbnail_variants'
    >
    fullUrl: string
    displayEdge?: number
  }

  let { asset, fullUrl, displayEdge: requestedDisplayEdge }: Props = $props()
  let imageElement: HTMLImageElement
  let currentSrc = $state('')
  let sources = $state<ProgressiveImageSource[]>([])
  let currentSource = $state<ProgressiveImageSource | null>(null)

  $effect(() => {
    const nextSources = thumbnailSourcesForAsset(asset)
    const nextSource = nextSources[0] ?? {
      url: asset.thumbnail_url ?? '',
      width: asset.thumbnail_width ?? 0,
      height: asset.thumbnail_height ?? 0,
      size: Math.max(asset.thumbnail_width ?? 0, asset.thumbnail_height ?? 0),
    }
    sources = nextSources
    currentSource = nextSource
    currentSrc = nextSource.url
  })

  onMount(() => {
    let cancelled = false
    const requestedUrls = new Set<string>()
    let idleId: number | undefined
    let timeoutId: ReturnType<typeof setTimeout> | undefined
    let resizeObserver: ResizeObserver | undefined

    async function maybeLoadBetterImage() {
      await tick()
      if (cancelled || currentSource === null || currentSource.original) return
      const bounds = imageElement.parentElement?.getBoundingClientRect() ?? imageElement.getBoundingClientRect()
      const displayEdge = requestedDisplayEdge ?? Math.max(bounds.width, bounds.height)
      const nextSource = nextProgressiveImageSource(
        sources,
        currentSource,
        displayEdge,
        fullUrl,
        asset.media_type
      )
      if (nextSource === null || requestedUrls.has(nextSource.url)) return
      requestedUrls.add(nextSource.url)

      const loadNextImage = () => {
        const image = new Image()
        image.decoding = 'async'
        image.onload = () => {
          if (cancelled || currentSource === null || nextSource.size <= currentSource.size) return
          currentSource = nextSource
          currentSrc = nextSource.url
          void maybeLoadBetterImage()
        }
        image.src = nextSource.url
      }

      if ('requestIdleCallback' in window) {
        idleId = window.requestIdleCallback(loadNextImage, { timeout: 1200 })
      } else {
        timeoutId = globalThis.setTimeout(loadNextImage, 0)
      }
    }

    void maybeLoadBetterImage()
    if ('ResizeObserver' in window) {
      resizeObserver = new ResizeObserver(() => {
        void maybeLoadBetterImage()
      })
      resizeObserver.observe(imageElement)
    }

    return () => {
      cancelled = true
      resizeObserver?.disconnect()
      if (idleId !== undefined && 'cancelIdleCallback' in window) window.cancelIdleCallback(idleId)
      globalThis.clearTimeout(timeoutId)
    }
  })
</script>

<img bind:this={imageElement} src={currentSrc} alt="" loading="lazy" decoding="async" />
