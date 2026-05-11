<script lang="ts">
  import { onMount, tick } from 'svelte'
  import type { Asset } from './result-cards'

  type Props = {
    asset: Pick<Asset, 'media_type' | 'thumbnail_width' | 'thumbnail_height'>
    thumbnailUrl: string
    fullUrl: string
  }

  let { asset, thumbnailUrl, fullUrl }: Props = $props()
  let imageElement: HTMLImageElement
  let currentSrc = $state('')

  $effect(() => {
    currentSrc = thumbnailUrl
  })

  onMount(() => {
    let cancelled = false
    let idleId: number | undefined
    let timeoutId: ReturnType<typeof setTimeout> | undefined

    async function maybeLoadFullImage() {
      await tick()
      if (cancelled || asset.media_type !== 'image' || !asset.thumbnail_width || !asset.thumbnail_height) return
      const bounds = imageElement.getBoundingClientRect()
      if (bounds.width <= asset.thumbnail_width && bounds.height <= asset.thumbnail_height) return

      const loadFullImage = () => {
        const fullImage = new Image()
        fullImage.decoding = 'async'
        fullImage.onload = () => {
          if (!cancelled) currentSrc = fullUrl
        }
        fullImage.src = fullUrl
      }

      if ('requestIdleCallback' in window) {
        idleId = window.requestIdleCallback(loadFullImage, { timeout: 1200 })
      } else {
        timeoutId = globalThis.setTimeout(loadFullImage, 0)
      }
    }

    void maybeLoadFullImage()

    return () => {
      cancelled = true
      if (idleId !== undefined && 'cancelIdleCallback' in window) window.cancelIdleCallback(idleId)
      globalThis.clearTimeout(timeoutId)
    }
  })
</script>

<img bind:this={imageElement} src={currentSrc} alt="" loading="lazy" decoding="async" />
