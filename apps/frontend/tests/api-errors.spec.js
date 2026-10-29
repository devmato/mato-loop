import { describe, expect, it } from 'vitest'

import { apiErrorMessage, decodeBlobApiError, errorDescriptor } from '../src/utils/api-errors'

describe('API error messages', () => {
  it('explains why a local worktree cannot start from an unborn branch', () => {
    expect(apiErrorMessage({ error: { code: 'git.localBranchRequired' } })).toBe(
      'Der lokale Zielbranch fehlt oder enthält noch keinen Commit. Prüfe den Zielbranch und erstelle bei Bedarf zuerst einen Commit.'
    )
  })
  it('explains why a dirty backend cannot supply an Agent installation target', () => {
    expect(apiErrorMessage({ error: { code: 'maintenance.targetUnavailable' } })).toBe(
      'Für diesen Backend-Stand ist keine reproduzierbare Agent-Version verfügbar. Verwende einen veröffentlichten Build oder einen lokalen Entwicklungs-Checkout.'
    )
  })
  it('explains how to retry a session without a computer', () => {
    expect(apiErrorMessage({ error: { code: 'session.computerRequired' } })).toBe(
      'Plan und Build benötigen einen Computer. Wähle einen verbundenen Computer und ein Arbeitsverzeichnis in den Session-Einstellungen. Starte diese Anfrage danach erneut.'
    )
  })
  it('explains a missing tool argument using controlled reason metadata', () => {
    expect(apiErrorMessage({ error: { code: 'tool.invalidArguments', params: { reason: 'missing' } } })).toBe(
      'Das Modell hat ein erforderliches Tool-Argument ausgelassen.'
    )
  })
  it('explains unavailable native audio analysis', () => {
    expect(apiErrorMessage({ error: { code: 'audio.analysisUnavailable' } })).toBe(
      'Für die direkte Audioanalyse ist kein audiofähiges Chatmodell verfügbar.'
    )
  })
  it('localizes an invalid computer-switch batch without leaking backend details', () => {
    expect(apiErrorMessage({ error: {
      code: 'executionAgent.exclusiveCallRequired', message: 'internal detail'
    } })).toBe('Der Computerwechsel muss ohne weitere Tool-Aufrufe erfolgen.')
  })
  it.each(['browser.snapshotChanged', 'browser.observationUnstable'])('reinspects instead of reconnecting for %s', (code) => {
    const message = apiErrorMessage({ error: { code, message: 'private worker detail' } })
    expect(message).toContain('Prüfe denselben Tab erneut')
    expect(message).not.toContain('private worker detail')
    expect(message).not.toContain('verbinde')
    expect(message).not.toContain('Öffne den Browser')
  })
  it('distinguishes a locked browser profile without suggesting reinstall or reset', () => {
    const message = apiErrorMessage({ error: { code: 'browser.profileInUse', message: 'private profile path' } })
    expect(message).toContain('gesperrt')
    expect(message).toContain('nicht zurückgesetzt')
    expect(message).not.toContain('private profile path')
    expect(message).not.toContain('Einrichtung')
  })
  it('uses controlled shell-inspection messages instead of parser details', () => {
    expect(apiErrorMessage({ error: { code: 'process.invalidSyntax', message: 'private parser detail' } })).toBe(
      'Der Shell-Befehl enthält einen Syntaxfehler. Korrigiere den Befehl vor einem erneuten Versuch.'
    )
    expect(apiErrorMessage({ error: { code: 'process.inspectionLimitExceeded', message: 'private path detail' } })).toBe(
      'Der Shell-Befehl überschreitet die Analysegrenzen. Teile ihn in kleinere Befehle auf.'
    )
    expect(apiErrorMessage({ error: { code: 'process.unsupportedTargetQuoting', message: 'private path detail' } })).toBe(
      'Der Shell-Zielpfad verwendet eine nicht unterstützte Maskierung. Verwende einen festen Pfad mit normalen Anführungszeichen.'
    )
  })
  it('formats failed commands without exposing an external error message', () => {
    expect(apiErrorMessage({ error: { code: 'process.commandFailed', message: 'private process detail' } })).toBe(
      'Der Befehl wurde mit einem Fehler beendet. Prüfe die Ausgabe und den Exit-Code.'
    )
  })
  it('translates subagent limits and read-only session errors', () => {
    expect(apiErrorMessage({ error: { code: 'agent.subagentLimitReached' } })).toBe(
      'Die maximale Anzahl an Subagents für diesen Lauf ist erreicht.'
    )
    expect(apiErrorMessage({ error: { code: 'session.readOnly' } })).toBe(
      'Subagent-Sessions sind schreibgeschützt. Arbeite in der Hauptsession weiter.'
    )
  })
  it('warns that an incomplete patch may have mutated even its failed operation', () => {
    expect(apiErrorMessage({ error: {
      code: 'file.patchIncomplete',
      message: 'private remote detail',
      params: { completedOperations: 0, totalOperations: 2, failedOperationIndex: 0, path: '/tmp/first' }
    } })).toBe(
      'Der Patch wurde nicht vollständig ausgeführt. Dateien können bereits geändert worden sein, auch durch den fehlgeschlagenen Schritt. Lies die betroffenen Pfade vor einem erneuten Versuch erneut ein.'
    )
  })

  it('distinguishes denied and unavailable file access', () => {
    expect(apiErrorMessage({ error: { code: 'file.accessDenied', message: 'private detail' } })).toBe(
      'Der Zugriff auf die Datei wurde verweigert.'
    )
    expect(apiErrorMessage({ error: { code: 'file.unavailable', message: 'private detail' } })).toBe(
      'Die Datei ist derzeit nicht verfügbar.'
    )
  })

  it('translates stable domain error codes', () => {
    expect(apiErrorMessage({ error: { code: 'secret.valueRefUnavailable' } })).toBe(
      'Der geschützte Wert ist nicht mehr verfügbar. Bitte erneut eingeben.'
    )
    expect(apiErrorMessage({ error: { code: 'permission.preflightUnavailable' } })).toBe(
      'Dieser Zugriff konnte nicht vorab vorbereitet werden. Bei der Ausführung kann eine Freigabe erforderlich sein.'
    )
    expect(apiErrorMessage({ error: { code: 'tool.superseded' } })).toBe(
      'Die ausstehende Aktion wurde wegen deiner neuen Nachricht übersprungen.'
    )
    expect(apiErrorMessage({ error: { code: 'approval.targetChanged' } })).toBe(
      'Das Ziel der Freigabe wurde inzwischen geändert. Der Vorschlag ist nicht mehr gültig.'
    )
    expect(apiErrorMessage({ response: { data: { error: { code: 'auth.invalidCredentials' } } } })).toBe(
      'Benutzername oder Passwort ist ungültig.'
    )
    expect(apiErrorMessage({ error: { code: 'integration.searchUnavailable' } })).toBe(
      'Richte zuerst eine aktive Websuche mit Standardanbieter ein.'
    )
    expect(apiErrorMessage({ error: { code: 'integration.remoteChanged' } })).toBe(
      'Der entfernte Eintrag wurde zwischenzeitlich geändert. Lade ihn erneut.'
    )
    expect(apiErrorMessage({ error: { code: 'integration.permissionDenied' } })).toBe(
      'Der verbundene Dienst erlaubt diese Aktion nicht.'
    )
    expect(apiErrorMessage({ error: { code: 'mcp.protocolError', message: 'private server detail' } })).toBe(
      'Der MCP-Server hat einen Protokollfehler gemeldet.'
    )
    expect(apiErrorMessage({ error: { code: 'mcp.endpointNotFound' } })).toBe(
      'Der MCP-Endpunkt wurde nicht gefunden. Prüfe die Server-URL und den Pfad.'
    )
    expect(apiErrorMessage({ error: { code: 'mcp.toolNotFound' } })).toContain('Tools des Servers')
    expect(apiErrorMessage({ error: { code: 'mcp.outcomeUnknown', message: 'private transport detail' } })).toContain('nicht blind')
    expect(apiErrorMessage({ error: { code: 'mcp.authorizationRequired', message: 'private OAuth detail' } })).toContain('MCP-Einstellungen')
    for (const code of ['mcp.oauthConfigurationInvalid', 'mcp.oauthFailed', 'mcp.oauthDenied', 'mcp.oauthExpired', 'mcp.oauthBusy']) {
      expect(apiErrorMessage({ error: { code, message: 'private OAuth detail' } })).not.toContain('private OAuth detail')
      expect(apiErrorMessage({ error: { code } })).not.toBe('Die Aktion ist fehlgeschlagen.')
    }
    for (const code of ['mcp.callFailed', 'mcp.catalogTooLarge', 'mcp.configurationInvalid', 'mcp.discoveryFailed', 'mcp.invalidCatalog', 'mcp.notFound', 'mcp.responseTooLarge', 'mcp.unavailable']) {
      expect(apiErrorMessage({ error: { code } })).not.toBe('Die Aktion ist fehlgeschlagen.')
    }
    expect(apiErrorMessage({ error: { code: 'integration.adapterDisabled' } })).toBe(
      'Dieser Integrationstyp ist derzeit nicht verfügbar.'
    )
    expect(apiErrorMessage({ error: { code: 'provider.discoveryUnsupported' } })).toBe(
      'Dieser Provider unterstützt keine automatische Modellerkennung.'
    )
    expect(apiErrorMessage({ error: { code: 'provider.notConnected' } })).toBe(
      'Der Provider ist nicht verbunden.'
    )
    expect(apiErrorMessage({ error: { code: 'provider.modelCapabilityUnavailable' } })).toBe(
      'Das ausgewählte Modell unterstützt die benötigte Fähigkeit nicht.'
    )
    expect(apiErrorMessage({ error: { code: 'provider.imageLimitExceeded', params: { maxImages: 4 } } })).toBe(
      'Eine Modellanfrage kann höchstens 4 Bilder enthalten.'
    )
    expect(apiErrorMessage({ error: { code: 'oauth.loginExpired' } })).toBe(
      'Der ChatGPT-Einmalcode ist abgelaufen. Bitte starte die Anmeldung erneut.'
    )
    expect(apiErrorMessage({ error: { code: 'openscad.renderFailed' } })).toBe(
      'Das OpenSCAD-Modell konnte nicht gerendert werden.'
    )
    expect(apiErrorMessage({ error: { code: 'project.notFound' } })).toBe(
      'Das Projekt wurde nicht gefunden.'
    )
    expect(apiErrorMessage({ error: { code: 'project.notEmpty' } })).toBe(
      'Das Projekt enthält noch Sessions.'
    )
    expect(apiErrorMessage({ error: { code: 'project.codingBindingImmutable' } })).toBe(
      'Die OpenCode-Zuordnung kann nach dem ersten gespeicherten Plan nicht mehr geändert werden.'
    )
    expect(apiErrorMessage({ error: { code: 'project.codingModelVariantUnavailable' } })).toBe(
      'Die ausgewählte OpenCode-Modellvariante ist nicht verfügbar.'
    )
    expect(apiErrorMessage({ error: { code: 'webPush.unavailable' } })).toBe(
      'Push-Benachrichtigungen sind derzeit nicht verfügbar.'
    )
    for (const code of [
      'cache.exportFailed',
      'cache.invalidContent',
      'cache.invalidDescriptor',
      'cache.invalidPath',
      'cache.notFound',
      'openscad.alreadyConfigured',
      'openscad.invalidArtifact',
      'openscad.invalidArchive',
      'openscad.invalidExecutable',
      'openscad.notConfigured',
      'openscad.notFound',
      'openscad.previewFailed',
      'openscad.previewUnavailable',
      'openscad.projectTooLarge',
      'openscad.testFailed',
      'openscad.unavailable',
      'openscad.unsafeProject'
    ]) {
      expect(apiErrorMessage({ error: { code } })).not.toBe('Die Aktion ist fehlgeschlagen.')
    }
    expect(apiErrorMessage({ error: { code: 'workspace.invalidEncoding' } })).toBe(
      'Die Datei kann nur als UTF-8-Text gelesen werden.'
    )
    expect(apiErrorMessage({ error: { code: 'file.workspaceNotSelected' } })).toBe(
      'Für diese Session ist kein aktueller Ordner auf dem Computer ausgewählt.'
    )
    expect(apiErrorMessage({ error: { code: 'executionAgent.updateRequired' } })).toBe(
      'Der Computer-Agent unterstützt die benötigte Protokollfunktion nicht. Bitte aktualisiere den Agenten.'
    )
    expect(apiErrorMessage({ error: { code: 'file.pathAlreadyExists' } })).toBe(
      'Der exakte Zielpfad existiert bereits. Dateien werden nicht überschrieben.'
    )
    expect(apiErrorMessage({ error: { code: 'file.invalidPath' } })).toBe(
      'Der Pfad muss ein gültiger POSIX-Pfad sein.'
    )
    expect(apiErrorMessage({ error: { code: 'file.changed' } })).toBe(
      'Die Datei wurde seit dem Öffnen geändert. Öffne sie erneut und übernimm deine Änderungen in die aktuelle Version.'
    )
    expect(apiErrorMessage({ error: { code: 'file.activeDirty' } })).toBe(
      'Die Datei enthält ungespeicherte Änderungen und kann vom Agenten nicht überschrieben werden.'
    )
    expect(apiErrorMessage({ error: { code: 'file.editTextNotFound' } })).toBe(
      'Der zu ersetzende Text wurde in der Datei nicht gefunden.'
    )
    expect(apiErrorMessage({ error: { code: 'file.editTextNotUnique' } })).toBe(
      'Der zu ersetzende Text kommt in der Datei mehrfach vor und ist nicht eindeutig.'
    )
    expect(apiErrorMessage({ error: { code: 'file.omittedContent' } })).toBe(
      'Der Dateiinhalt fehlt in der Tool-Anfrage.'
    )
    expect(apiErrorMessage({ error: { code: 'file.userQuotaExceeded' } })).toBe(
      'Das Speicherlimit für Dateien dieses Benutzers ist erreicht.'
    )
    expect(apiErrorMessage({ error: { code: 'file.sessionQuotaExceeded' } })).toBe(
      'Das Speicherlimit für Dateien dieser Session ist erreicht.'
    )
    expect(apiErrorMessage({ error: { code: 'attachment.sessionQuotaExceeded' } })).toBe(
      'Das Speicherlimit für Dateien dieser Session ist erreicht.'
    )
    expect(apiErrorMessage({ error: { code: 'file.operationTooLarge' } })).toBe(
      'Die Quelle ist für eine einzelne Dateioperation zu groß oder enthält zu viele Einträge.'
    )
    expect(apiErrorMessage({ error: { code: 'session.storageUnavailable' } })).toBe(
      'Der interne Dateispeicher dieser Session ist derzeit nicht verfügbar.'
    )
    expect(apiErrorMessage({ error: { code: 'session.filesReferenced', params: { referenceCount: 2 } } })).toBe(
      '2 Dateireferenzen mit dieser Session verhindern den Vorgang.'
    )
    expect(apiErrorMessage({ error: { code: 'session.messageNotFound' } })).toBe(
      'Die Chatnachricht wurde nicht gefunden.'
    )
    expect(apiErrorMessage({ error: { code: 'session.retryUnavailable' } })).toBe(
      'Diese fehlgeschlagene Antwort kann nicht mehr erneut versucht werden.'
    )
    expect(apiErrorMessage({ error: { code: 'tool.cursorInvalid' } })).toBe(
      'Der referenzierte Nachrichtenabschnitt ist nicht mehr verfügbar.'
    )
    expect(apiErrorMessage({ error: { code: 'webfetch.addressBlocked' } })).toBe(
      'Diese Netzwerkadresse ist aus Sicherheitsgründen gesperrt.'
    )
    expect(apiErrorMessage({ error: { code: 'webfetch.emptyResponse' } })).toBe(
      'Die Webadresse hat keinen verwertbaren Inhalt geliefert.'
    )
    expect(apiErrorMessage({ error: { code: 'webfetch.tlsFailed' } })).toBe(
      'Das TLS-Zertifikat der Webadresse konnte nicht überprüft werden.'
    )
    expect(apiErrorMessage({ error: {
      code: 'webfetch.contentTypeUnsupported',
      message: 'private upstream detail',
      params: { statusCode: 200, contentType: 'application/zip' }
    } })).toBe('Dieser Inhaltstyp kann nicht abgerufen werden.')
    expect(apiErrorMessage({ error: { code: 'transcript.unavailable' } })).toBe(
      'Für dieses Video sind keine unterstützten veröffentlichten Untertitel verfügbar.'
    )
    expect(apiErrorMessage({ error: { code: 'attachment.tooLarge' } })).toBe(
      'Der Dateianhang ist zu groß.'
    )
    expect(apiErrorMessage({ error: { code: 'attachment.textTooLarge' } })).toBe(
      'Die Textdatei überschreitet das konfigurierte Verarbeitungslimit.'
    )
    expect(apiErrorMessage({ error: { code: 'question.answerTooLarge' } })).toBe(
      'Die Antworten überschreiten das zulässige Größenlimit.'
    )
    expect(apiErrorMessage({ error: {
      code: 'question.placeholderNotAllowed',
      message: 'private validation detail',
      field: 'questions[2].placeholder'
    } })).toBe(
      'Platzhalter sind nur bei Freitextfragen erlaubt. Bei Auswahlfragen muss das Modell den Platzhalter weglassen oder auf null setzen, auch wenn eigene Antworten erlaubt sind.'
    )
    expect(apiErrorMessage({ error: { code: 'attachment.invalidDocument' } })).toBe(
      'Das Dokument ist beschädigt, geschützt oder überschreitet die Verarbeitungsgrenzen.'
    )
    expect(apiErrorMessage({ error: { code: 'agent.requiredWebSearchMissing' } })).toBe(
      'Das Modell hat die verpflichtende Websuche auch nach mehreren Versuchen nicht ausgeführt.'
    )
    expect(apiErrorMessage({ error: { code: 'agent.requiredMemoryWriteMissing' } })).toBe(
      'Das Modell hat den begonnenen Memory-Schreibvorgang auch nach mehreren Versuchen nicht abgeschlossen.'
    )
    expect(apiErrorMessage({ error: { code: 'agent.requiredOpenScadSourceReviewMissing' } })).toBe(
      'Das Modell hat die gerenderte OpenSCAD-Datei auch nach mehreren Versuchen nicht geprüft.'
    )
    expect(apiErrorMessage({ error: { code: 'agent.researchSourcesInvalid' } })).toBe(
      'Die recherchierte Antwort konnte nicht ausreichend mit aktuellen Quellen belegt werden.'
    )
    expect(apiErrorMessage({ error: { code: 'agent.emptyResponse' } })).toBe(
      'Das Modell hat auch nach einem erneuten Versuch keine verwertbare Antwort geliefert.'
    )
    expect(apiErrorMessage({ error: { code: 'memory.dreamingModelUnavailable' } })).toBe(
      'Für Dreaming ist kein verfügbares Modell eingerichtet.'
    )
    expect(apiErrorMessage({ error: { code: 'memory.disabled' } })).toBe(
      'Memory ist deaktiviert.'
    )
    expect(apiErrorMessage({ error: { code: 'memory.duplicate' } })).toBe(
      'Diese Erinnerung ist bereits vorhanden.'
    )
    expect(apiErrorMessage({ error: { code: 'skill.draftInvalid' } })).toBe(
      'Der Skill-Entwurf ist ungültig. Prüfe die Entwurfsdatei und versuche es erneut.'
    )
    expect(apiErrorMessage({ error: { code: 'skill.creationModelFailed' } })).toBe(
      'Der Skill-Entwurf konnte vom ausgewählten Modell nicht erstellt werden.'
    )
    expect(apiErrorMessage({ error: { code: 'skill.changed' } })).toBe(
      'Der Skill wurde zwischenzeitlich geändert. Starte die Bearbeitung erneut.'
    )
    expect(apiErrorMessage({ error: { code: 'skill.notFound' } })).toBe(
      'Der Skill wurde nicht gefunden.'
    )
    expect(apiErrorMessage({ error: { code: 'skill.personalDisabled' } })).toBe(
      'Persönliche Skills sind für diese Instanz deaktiviert.'
    )
    expect(apiErrorMessage({ error: { code: 'skill.duplicate' } })).toBe(
      'Ein Skill mit diesem Namen ist bereits vorhanden.'
    )
    expect(apiErrorMessage({ error: { code: 'skill.limitReached' } })).toBe(
      'Die maximale Anzahl Skills in diesem Bereich ist erreicht.'
    )
    expect(apiErrorMessage({ error: { code: 'skill.inUse' } })).toBe(
      'Der Skill wird noch von einer Automation verwendet und kann nicht gelöscht werden.'
    )
    expect(apiErrorMessage({ error: { code: 'automation.scheduleInvalid' } })).toBe(
      'Der Zeitplan ist ungültig.'
    )
    expect(apiErrorMessage({ error: { code: 'automation.alreadyRunning' } })).toBe(
      'Für diese Automation läuft bereits ein Auftrag.'
    )
    expect(apiErrorMessage({ error: { code: 'session.archived' } })).toBe(
      'Stelle die archivierte Session oder ihr Projekt wieder her, bevor du neue Arbeit startest.'
    )
    expect(apiErrorMessage({
      error: { code: 'session.automationOutputRequired', params: { automationCount: 2 } }
    })).toBe('Diese Session enthält 2 Automationen. Bestätige das gemeinsame Löschen.')
    expect(apiErrorMessage({ error: { code: 'session.automationImpactChanged' } })).toBe(
      'Die Sessions oder verknüpften Automationen wurden zwischenzeitlich geändert. Bitte versuche es erneut.'
    )
    expect(apiErrorMessage({ error: { code: 'privacy.pseudonymizationUnavailable' } })).toBe(
      'Die lokale Pseudonymisierung ist derzeit nicht verfügbar.'
    )
    expect(apiErrorMessage({ error: { code: 'video.frameExtractionFailed' } })).toBe(
      'Die Einzelbilder des Videos konnten nicht extrahiert werden.'
    )
    expect(apiErrorMessage({
      error: { code: 'provider.timeout', params: { timeoutSeconds: 420, retryable: true } }
    })).toBe('Der Provider hat nicht innerhalb von 420 Sekunden geantwortet.')
  })

  it('formats validation parameters without backend text', () => {
    const source = {
      response: {
        data: {
          error: {
            code: 'validation.tooShort',
            field: 'password',
            params: { minLength: 10 }
          }
        }
      }
    }

    expect(apiErrorMessage(source)).toBe('Das Passwort muss mindestens 10 Zeichen lang sein.')
    expect(apiErrorMessage({
      error: {
        code: 'validation.tooManyItems',
        field: 'audio',
        params: { maxItems: 4 }
      }
    })).toBe('Die Audioauswahl darf höchstens 4 Einträge enthalten.')
    expect(apiErrorMessage({
      error: { code: 'validation.invalid', field: 'validUntil' }
    })).toBe('Das Gültigkeitsende ist ungültig.')
    expect(apiErrorMessage({
      error: { code: 'validation.invalid', field: 'expectedRevision' }
    })).toBe('Die Version der Erinnerung ist ungültig.')
    expect(apiErrorMessage({
      error: { code: 'validation.invalid', field: 'fullName' }
    })).toBe('Der vollständige Name ist ungültig.')
  })

  it('translates execution, process and transfer errors without unsafe retry advice', () => {
    const codes = [
      'executionAgent.alreadyPaired',
      'executionAgent.notFound',
      'executionAgent.operationUnavailable',
      'executionAgent.outcomeUnknown',
      'executionAgent.pairingTokenExpired',
      'executionAgent.pairingTokenInvalid',
      'executionAgent.protocolError',
      'executionAgent.replaced',
      'executionAgent.requestTooLarge',
      'executionAgent.responseTooLarge',
      'executionAgent.revoked',
      'executionAgent.timeout',
      'executionAgent.unavailable',
      'process.interrupted',
      'process.cancelled',
      'process.failed',
      'process.notAllowed',
      'process.outcomeUnknown',
      'process.outputTooLarge',
      'process.startFailed',
      'process.sudoUnavailable',
      'process.sudoAuthRequired',
      'process.sudoAuthFailed',
      'process.timedOut',
      'process.unavailable',
      'transfer.expired',
      'transfer.invalidContent',
      'transfer.invalidTicket',
      'transfer.tooLarge'
    ]

    for (const code of codes) {
      expect(apiErrorMessage({ error: { code } })).not.toBe('Die Aktion ist fehlgeschlagen.')
    }
    for (const code of [
      'executionAgent.outcomeUnknown',
      'process.outcomeUnknown',
      'integration.outcomeUnknown'
    ]) {
      expect(apiErrorMessage({ error: { code } })).toContain('nicht blind')
    }
  })

  it('never renders legacy validation payloads', () => {
    const source = {
      response: {
        status: 422,
        data: {
          detail: [{ type: 'string_too_short', loc: ['body', 'password'], input: 'secret' }]
        }
      }
    }

    expect(errorDescriptor(source)).toBeNull()
    expect(apiErrorMessage(source)).toBe('Bitte überprüfe deine Eingaben.')
  })

  it('uses safe fallbacks for unknown and network errors', () => {
    expect(apiErrorMessage({ error: { code: 'future.unknown' } }, 'Aktion fehlgeschlagen')).toBe(
      'Aktion fehlgeschlagen'
    )
    expect(apiErrorMessage({ request: {} })).toBe('MatoLoop ist nicht erreichbar.')
  })

  it('prefers German translations over backend messages', () => {
    expect(apiErrorMessage({
      error: {
        code: 'agent.requiredWebSearchMissing',
        message: 'The required web search was not performed.',
        params: {}
      }
    })).toBe('Das Modell hat die verpflichtende Websuche auch nach mehreren Versuchen nicht ausgeführt.')
    expect(apiErrorMessage({
      error: { code: 'future.unknown', message: 'Secret upstream details' }
    })).toBe('Die Aktion ist fehlgeschlagen.')
  })

  it('decodes structured blob errors before localizing them', async () => {
    for (const [code, message] of [
      [
        'file.changed',
        'Die Datei wurde seit dem Öffnen geändert. Öffne sie erneut und übernimm deine Änderungen in die aktuelle Version.'
      ],
      ['openscad.renderFailed', 'Das OpenSCAD-Modell konnte nicht gerendert werden.']
    ]) {
      const source = {
        response: {
          status: 409,
          data: new Blob([JSON.stringify({ error: { code } })], { type: 'application/json' })
        }
      }
      const decoded = await decodeBlobApiError(source)
      expect(decoded).toBe(source)
      expect(apiErrorMessage(decoded)).toBe(message)
    }
  })
})
