import { describe, expect, it } from 'vitest'
import {
  type Asset,
  type CollectionCard,
  cardContainsAsset,
  collectionColor,
  visibleEntriesForCards,
} from './result-cards'

function asset(id: string): Asset {
  return {
    asset_id: id,
    source_id: 'sample',
    source_display_name: 'Sample Assets',
    relative_path: `maps/${id}.png`,
    filename: `${id}.png`,
    stem: id,
    extension: '.png',
    directory: 'maps',
    size_bytes: 2048,
    media_type: 'image',
  }
}

describe('result card helpers', () => {
  it('represents collapsed collections as a single visible entry', () => {
    const collection: CollectionCard = {
      kind: 'collection',
      collection_key: 'maps/castle',
      representative: asset('castle-day'),
      variants: [asset('castle-day'), asset('castle-night')],
      count: 2,
    }

    expect(visibleEntriesForCards([collection], new Set())).toEqual([
      { kind: 'collection', collection },
    ])
  })

  it('expands collections into a header and member assets', () => {
    const collection: CollectionCard = {
      kind: 'collection',
      collection_key: 'maps/castle',
      representative: asset('castle-day'),
      variants: [asset('castle-day'), asset('castle-night')],
      count: 2,
    }

    expect(visibleEntriesForCards([collection], new Set(['maps/castle']))).toMatchObject([
      { kind: 'collection-header', collection },
      { kind: 'asset', asset: { asset_id: 'castle-day' }, collection_key: 'maps/castle' },
      { kind: 'asset', asset: { asset_id: 'castle-night' }, collection_key: 'maps/castle' },
    ])
  })

  it('checks both plain asset cards and collection variants for membership', () => {
    const plain = { kind: 'asset' as const, asset: asset('plain-map') }
    const collection: CollectionCard = {
      kind: 'collection',
      collection_key: 'maps/castle',
      representative: asset('castle-day'),
      variants: [asset('castle-day'), asset('castle-night')],
      count: 2,
    }

    expect(cardContainsAsset(plain, 'plain-map')).toBe(true)
    expect(cardContainsAsset(collection, 'castle-night')).toBe(true)
    expect(cardContainsAsset(collection, 'missing')).toBe(false)
  })

  it('assigns stable collection colors', () => {
    expect(collectionColor('maps/castle')).toBe(collectionColor('maps/castle'))
    expect(collectionColor('maps/castle')).toMatch(/^#[0-9a-f]{6}$/)
  })
})
