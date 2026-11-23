// REST reads allocate fresh objects even when the visible data is unchanged.
export function equalJson(left, right) {
  if (left === right) return true
  if (!left || !right || typeof left !== 'object' || typeof right !== 'object'
    || Array.isArray(left) !== Array.isArray(right)) return false
  const keys = Object.keys(right)
  return Object.keys(left).length === keys.length
    && keys.every(key => Object.hasOwn(left, key) && equalJson(left[key], right[key]))
}

export function reuseList(previous, next) {
  return previous && previous.length === next.length && next.every((item, index) => item === previous[index])
    ? previous : next
}

export function reconcileApiRows(previous, incoming) {
  const byId = new Map(previous.map(item => [item.id, item]))
  return reuseList(previous, incoming.map(item => {
    const old = byId.get(item.id)
    return equalJson(old, item) ? old : item
  }))
}
