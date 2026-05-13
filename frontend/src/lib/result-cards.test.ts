import { describe, expect, it } from 'vitest'
import {
  type Asset,
  type CollectionCard,
  cardContainsAsset,
  collectionColor,
  nextProgressiveImageSource,
  thumbnailSourcesForAsset,
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

  it('chooses the smallest thumbnail variant that fits the displayed card', () => {
    const sources = thumbnailSourcesForAsset({
      ...asset('castle'),
      thumbnail_variants: [
        { max_edge: 256, width: 256, height: 128, url: '/thumb-256.webp' },
        { max_edge: 512, width: 512, height: 256, url: '/thumb-512.webp' },
        { max_edge: 1024, width: 1024, height: 512, url: '/thumb-1024.webp' },
      ],
    })

    expect(nextProgressiveImageSource(sources, sources[0], 400)?.url).toBe('/thumb-512.webp')
  })

  it('does not jump from a small thumbnail to a much larger source for modest growth', () => {
    const sources = thumbnailSourcesForAsset({
      ...asset('castle'),
      thumbnail_variants: [
        { max_edge: 256, width: 256, height: 128, url: '/thumb-256.webp' },
        { max_edge: 1024, width: 1024, height: 512, url: '/thumb-1024.webp' },
      ],
    })

    expect(nextProgressiveImageSource(sources, sources[0], 400)).toBeNull()
    expect(nextProgressiveImageSource(sources, sources[0], 600)?.url).toBe('/thumb-1024.webp')
  })

  it('uses the original image only when image cards outgrow every thumbnail variant', () => {
    const sources = thumbnailSourcesForAsset({
      ...asset('castle'),
      thumbnail_variants: [
        { max_edge: 256, width: 256, height: 128, url: '/thumb-256.webp' },
      ],
    })

    expect(
      nextProgressiveImageSource(sources, sources[0], 600, '/file.png', 'image')?.url
    ).toBe('/file.png')
    expect(nextProgressiveImageSource(sources, sources[0], 600, '/file.mp4', 'video')).toBeNull()
  })
})
