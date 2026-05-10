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
  thumbnail_url?: string
  thumbnail_width?: number
  thumbnail_height?: number
  score?: number
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

