import { Dialog } from 'quasar'

export function useConfirm() {
  function confirm(
    message = 'Soll die Aktion wirklich ausgeführt werden?',
    title = 'Bestätigung',
    { okLabel = 'Bestätigen', okColor = 'negative', cancelLabel = 'Abbrechen' } = {}
  ) {
    return new Promise((resolve) => {
      Dialog.create({
        transitionDuration: 0,
        title,
        message,
        ok: { label: okLabel, color: okColor, unelevated: true, noCaps: true },
        cancel: { label: cancelLabel, color: undefined, class: 'app-muted', flat: true, noCaps: true }
      })
        .onOk(() => resolve(true))
        .onCancel(() => resolve(false))
        .onDismiss(() => resolve(false))
    })
  }

  return { confirm }
}
