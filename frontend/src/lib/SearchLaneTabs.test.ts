import { render, screen } from '@testing-library/svelte'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import SearchLaneTabs from './SearchLaneTabs.svelte'

describe('SearchLaneTabs', () => {
  it('marks the selected lane and reports lane changes', async () => {
    const user = userEvent.setup()
    const onSelect = vi.fn()

    render(SearchLaneTabs, { value: 'strict', onSelect })

    expect(screen.getByRole('tab', { name: 'Exact' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tab', { name: 'Fuzzy' })).toHaveAttribute('aria-selected', 'false')

    await user.click(screen.getByRole('tab', { name: 'Domain' }))

    expect(onSelect).toHaveBeenCalledWith('domain')
  })
})

