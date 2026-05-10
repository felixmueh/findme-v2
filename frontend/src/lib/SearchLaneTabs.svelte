<script lang="ts">
  export type SearchMode = 'strict' | 'fuzzy' | 'domain'

  type Props = {
    value: SearchMode
    onSelect: (mode: SearchMode) => void
  }

  let { value, onSelect }: Props = $props()

  const modes: Array<{ id: SearchMode; label: string }> = [
    { id: 'strict', label: 'Exact' },
    { id: 'fuzzy', label: 'Fuzzy' },
    { id: 'domain', label: 'Domain' },
  ]
</script>

<div class="lane-tabs" role="tablist" aria-label="Search lane">
  {#each modes as mode}
    <button
      class:active={value === mode.id}
      type="button"
      role="tab"
      aria-selected={value === mode.id}
      onclick={() => onSelect(mode.id)}
    >
      {mode.label}
    </button>
  {/each}
</div>

