// A read/write transaction lets tabs and the service worker claim the same notice only once.
export async function claimNotificationSound(ids, play = () => true) {
  const database = await new Promise((resolve, reject) => {
    const request = globalThis.indexedDB.open('matoLoopNotificationSound', 1)
    request.onupgradeneeded = () => request.result.createObjectStore('claims')
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error)
  })
  try {
    return await new Promise((resolve, reject) => {
      const transaction = database.transaction('claims', 'readwrite')
      const store = transaction.objectStore('claims')
      const request = store.get('recent')
      let claimed = false
      request.onsuccess = () => {
        const recent = new Set(request.result || [])
        const fresh = ids.filter((id) => !recent.has(id))
        if (!fresh.length || !play()) return
        claimed = true
        store.put([...recent, ...fresh].slice(-256), 'recent')
      }
      transaction.oncomplete = () => resolve(claimed)
      // If playback already started, don't retry it on a storage failure.
      transaction.onabort = () => claimed ? resolve(true) : reject(transaction.error)
    })
  } finally {
    database.close()
  }
}
