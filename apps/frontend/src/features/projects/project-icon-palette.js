export const projectIconPalette = [
  { value: 'blue', label: 'Blau', light: '#1d4ed8', dark: '#93c5fd' },
  { value: 'teal', label: 'Türkis', light: '#0e667e', dark: '#67e8f9' },
  { value: 'green', label: 'Grün', light: '#056c4e', dark: '#6ee7b7' },
  { value: 'lime', label: 'Limette', light: '#416c0b', dark: '#bef264' },
  { value: 'ochre', label: 'Ocker', light: '#854d0e', dark: '#fde047' },
  { value: 'orange', label: 'Orange', light: '#ad380a', dark: '#fdba74' },
  { value: 'red', label: 'Rot', light: '#b91c1c', dark: '#fca5a5' },
  { value: 'pink', label: 'Pink', light: '#be185d', dark: '#f9a8d4' },
  { value: 'violet', label: 'Violett', light: '#6d28d9', dark: '#c4b5fd' },
  { value: 'slate', label: 'Schiefergrau', light: '#475569', dark: '#cbd5e1' }
]

export function projectIconTone(index, color) {
  const selected = projectIconPalette.find(item => item.value === color)
  return selected ?? (index == null ? undefined : projectIconPalette[index % projectIconPalette.length])
}
