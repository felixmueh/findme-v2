export type ThumbnailVariant = {
  kind?: string
  max_edge: number
  width: number
  height: number
  url: string
}

export type Asset = {
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
  media_width?: number | null
  media_height?: number | null
  duration_seconds?: number | null
  thumbnail_url?: string
  thumbnail_width?: number
  thumbnail_height?: number
  thumbnail_variants?: ThumbnailVariant[]
  score?: number
}

export type ProgressiveImageSource = {
  url: string
  width: number
  height: number
  size: number
  original?: boolean
}

export type AssetCard = {
  kind: 'asset'
  asset: Asset
}

export type CollectionCard = {
  kind: 'collection'
  collection_key: string
  representative: Asset
  variants: Asset[]
  count: number
}

export type ResultCard = AssetCard | CollectionCard

export type VisibleEntry =
  | { kind: 'asset'; asset: Asset; collection_key?: string }
  | { kind: 'collection'; collection: CollectionCard }
  | { kind: 'collection-header'; collection: CollectionCard }

export function visibleEntriesForCards(resultCards: ResultCard[], expanded: Set<string>) {
  const entries: VisibleEntry[] = []
  for (const card of resultCards) {
    if (card.kind === 'asset') {
      entries.push({ kind: 'asset', asset: card.asset })
      continue
    }
    if (!expanded.has(card.collection_key)) {
      entries.push({ kind: 'collection', collection: card })
      continue
    }
    entries.push({ kind: 'collection-header', collection: card })
    for (const asset of card.variants) {
      entries.push({
        kind: 'asset',
        asset,
        collection_key: card.collection_key,
      })
    }
  }
  return entries
}

export function cardContainsAsset(card: ResultCard, assetId: string) {
  if (card.kind === 'asset') return card.asset.asset_id === assetId
  return card.variants.some((asset) => asset.asset_id === assetId)
}

export function collectionColor(collectionKey: string) {
  const colors = ['#c2410c', '#0f766e', '#7c3aed', '#b45309', '#be123c', '#4d7c0f']
  let hash = 0
  for (let index = 0; index < collectionKey.length; index += 1) {
    hash = (hash * 31 + collectionKey.charCodeAt(index)) >>> 0
  }
  return colors[hash % colors.length]
}

export function thumbnailSourcesForAsset(
  asset: Pick<
    Asset,
    'thumbnail_variants' | 'thumbnail_url' | 'thumbnail_width' | 'thumbnail_height'
  >
): ProgressiveImageSource[] {
  const sources = (asset.thumbnail_variants ?? [])
    .map((variant) => ({
      url: variant.url,
      width: variant.width,
      height: variant.height,
      size: variant.max_edge,
    }))
    .sort((left, right) => left.size - right.size)

  if (sources.length > 0) return dedupeImageSources(sources)
  if (!asset.thumbnail_url || !asset.thumbnail_width || !asset.thumbnail_height) return []

  return [
    {
      url: asset.thumbnail_url,
      width: asset.thumbnail_width,
      height: asset.thumbnail_height,
      size: Math.max(asset.thumbnail_width, asset.thumbnail_height),
    },
  ]
}

export function nextProgressiveImageSource(
  sources: ProgressiveImageSource[],
  current: ProgressiveImageSource,
  displayWidth: number,
  originalUrl?: string,
  mediaType?: string
): ProgressiveImageSource | null {
  if (displayWidth <= current.size) return null

  const next = sources.find((source) => source.size > current.size)
  if (next) {
    if (next.size <= current.size * 2 + 100 || displayWidth > current.size * 2) {
      return next
    }
    return null
  }

  if (mediaType === 'image' && originalUrl && displayWidth > current.size * 2) {
    return {
      url: originalUrl,
      width: current.width,
      height: current.height,
      size: Number.POSITIVE_INFINITY,
      original: true,
    }
  }

  return null
}

function dedupeImageSources(sources: ProgressiveImageSource[]) {
  const seen = new Set<string>()
  const deduped: ProgressiveImageSource[] = []
  for (const source of sources) {
    const key = `${source.size}:${source.url}`
    if (seen.has(key)) continue
    seen.add(key)
    deduped.push(source)
  }
  return deduped
}
