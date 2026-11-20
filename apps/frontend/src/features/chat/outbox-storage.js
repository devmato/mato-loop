const DATABASE_NAME = 'matoLoopChatOutbox'
const DATABASE_VERSION = 1
const STORE_NAME = 'entries'

export function compareChatOutboxEntries(left, right) {
  return Number(Boolean(right.interrupt)) - Number(Boolean(left.interrupt))
    || Number(Boolean(right.serverQueued)) - Number(Boolean(left.serverQueued))
    || (left.serverQueued && right.serverQueued ? left.queuePosition - right.queuePosition : 0)
    || left.queuedAt.localeCompare(right.queuedAt)
    || left.id.localeCompare(right.id)
}

function openDatabase() {
  if (!globalThis.indexedDB) return Promise.reject(new Error('IndexedDB is unavailable'))
  return new Promise((resolve, reject) => {
    const request = globalThis.indexedDB.open(DATABASE_NAME, DATABASE_VERSION)
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(STORE_NAME)) {
        request.result.createObjectStore(STORE_NAME, { keyPath: 'id' })
      }
    }
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error)
  })
}

async function runTransaction(mode, operation) {
  const database = await openDatabase()
  return new Promise((resolve, reject) => {
    const transaction = database.transaction(STORE_NAME, mode)
    let result
    transaction.oncomplete = () => {
      database.close()
      resolve(result)
    }
    transaction.onerror = () => {
      database.close()
      reject(transaction.error)
    }
    transaction.onabort = () => {
      database.close()
      reject(transaction.error || new Error('Outbox transaction was aborted'))
    }
    try {
      operation(transaction.objectStore(STORE_NAME), (value) => { result = value })
    } catch (error) {
      transaction.abort()
      reject(error)
    }
  })
}

export async function loadChatOutbox(ownerUserId) {
  if (!ownerUserId) return []
  return runTransaction('readonly', (store, done) => {
    const request = store.getAll()
    request.onsuccess = () => {
      // shortcut: queues are small; add an owner index if this scan becomes measurable.
      done(request.result
        .filter((entry) => entry.ownerUserId === ownerUserId)
        .sort(compareChatOutboxEntries))
    }
  })
}

export function saveChatOutboxEntry(entry) {
  return runTransaction('readwrite', (store) => { store.put(entry) })
}

export function saveChatOutboxEntries(entries) {
  return runTransaction('readwrite', (store) => {
    for (const entry of entries) store.put(entry)
  })
}

export function deleteChatOutboxEntry(id) {
  return runTransaction('readwrite', (store) => { store.delete(id) })
}

export function pauseChatOutboxEntries(ids) {
  return runTransaction('readwrite', (store) => {
    for (const id of ids) {
      const request = store.get(id)
      request.onsuccess = () => {
        if (request.result) store.put({ ...request.result, paused: true })
      }
    }
  })
}

export function deleteChatOutboxEntries(ids) {
  return runTransaction('readwrite', (store) => {
    for (const id of ids) store.delete(id)
  })
}

export async function clearChatOutbox(ownerUserId) {
  if (!ownerUserId) return
  await runTransaction('readwrite', (store) => {
    const request = store.openCursor()
    request.onsuccess = () => {
      const cursor = request.result
      if (!cursor) return
      if (cursor.value.ownerUserId === ownerUserId) cursor.delete()
      cursor.continue()
    }
  })
}
