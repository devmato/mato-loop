const refreshes = new WeakMap()

export function queueRefresh(store, config, read) {
  const previous = refreshes.get(store)
  if (previous?.queued && previous.revision === store.revision
    && Object.keys(config || {}).length === Object.keys(previous.config || {}).length
    && Object.keys(config || {}).every((key) => config[key] === previous.config[key])) return previous.promise

  const revision = ++store.revision
  const next = { config, revision, queued: Boolean(previous), promise: null }
  const run = () => {
    next.queued = false
    if (revision !== store.revision) return
    return read(revision)
  }
  const request = previous ? previous.promise.catch(() => {}).then(run) : run()
  next.promise = Promise.resolve(request).finally(() => {
    if (refreshes.get(store) === next) refreshes.delete(store)
  })
  refreshes.set(store, next)
  return next.promise
}
