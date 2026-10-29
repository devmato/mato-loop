# Webrecherche und Quellen

## Websuchmodus der Session

| Modus | Verhalten |
| --- | --- |
| Erzwingen | Vor der normalen Antwort muss eine erfolgreiche Websuche stattfinden |
| Verfügbar | Der Agent kann bei Bedarf suchen |
| Deaktiviert | Das Suchwerkzeug wird nicht angeboten |

Die Auswahl des tatsächlichen Suchproviders erfolgt durch MatoLoop und kann vom Modell nicht überschrieben werden.

## Suchprovider

Ohne weitere Einrichtung verwendet MatoLoop eine eingebaute Suche ohne API-Schlüssel. Zusätzliche SearXNG-Instanzen werden ausschließlich global unter **Administration > Integrationen** verwaltet. Je einen Anbieter für Websuche und Webabrufe wählen Sie unter **Administration > Tools > Einstellungen**. Persönliche Anbieterwahl wird nicht unterstützt.

Bei Fehlern gibt es keinen automatischen Anbieterwechsel. Alte Fallback-Werte werden ignoriert; der bisherige Primäranbieter bleibt ausgewählt.

## Webseiten abrufen

Unter **Einstellungen > Grundeinstellungen > Bevorzugte Sprache** hinterlegen Sie
eine persönliche Sprachvorgabe. Sie gilt für Transkriptionen und die Browser-Sprache
von Jina Reader. **Automatisch** behält die Erkennung beziehungsweise Dienstvorgabe
bei. Ausdrückliche Sprachangaben beim Aufruf haben Vorrang. Die Einstellung übersetzt
keine Inhalte und ändert nicht die Oberflächensprache.

`webfetch` ruft eine konkrete HTTP- oder HTTPS-Adresse ab und ist unabhängig von der Websuche konfiguriert. Das ausgewählte Agentenprofil legt dafür getrennt für private und öffentliche Modelle eine Richtlinie fest:

- **Deaktiviert**
- **Nachfragen**
- **Automatisch**

Administratorinnen und Administratoren konfigurieren die Verfügbarkeit unter **Tools > Tool-Einstellungen**. Unter **Tools > Einstellungen > Webabrufe und Downloads** wählen sie instanzweit einen Anbieter. Jina übernimmt Abruf und JavaScript-Verarbeitung selbst; seine Zielanfragen unterliegen nicht MatoLoops lokaler Adressprüfung. Der eingebaute Anbieter führt kein JavaScript aus und prüft jede Weiterleitung. Für direkte Abrufe ist **Nur öffentliche Zieladressen zulassen** unter **Tools > Einstellungen > Webabrufe und Downloads** standardmäßig aktiv. Das Abschalten erlaubt auch private vom Backend erreichbare Ziele. Große Ergebnisse bleiben vollständig gespeichert; fehlende Abschnitte können über den Ergebnisverweis ohne erneuten Abruf gelesen werden. Browsersteuerung dient nötiger Seiteninteraktion, nicht als automatische Antwort auf jede unbrauchbare Quelle.

Für Bildreferenzen kann der Agent Bildsuche verwenden und muss die konkrete Bildadresse vor Verwendung abrufen. Ein leerer Webfetch-Inhalt gilt als kontrollierter Fehler; ein identischer Wiederholungsversuch ist erlaubt, ohne automatische Browserübernahme. Originaldokumente können ausdrücklich als Datei in der Ablage heruntergeladen werden.

Bei ausgewähltem Jina Reader kann der Agent gezielt den Abrufmodus (`auto`,
`browser`, `curl`), Overlay-Entfernung, Cache-Umgehung, Inhalts-/Warte-/Ausschlussselektoren
sowie Link- und Bildübersichten wählen. Browser-Modus ermöglicht JavaScript,
erteilt aber keine Zustimmung und löst keine Login- oder Bot-Sperren automatisch.
Link- und Bildübersichten sind keine bereits abgerufenen Bilder oder geprüften Quellen.

## Veröffentlichte Untertitel

`fetch_transcript` ruft vorhandene veröffentlichte Untertitel unterstützter öffentlicher YouTube-Videos ab und verwendet dieselbe Webzugriffsrichtlinie. Das Werkzeug lädt kein Video herunter, übersetzt keine Untertitel und startet keine automatische Transkription. Ist eine gewünschte Sprache nicht vorhanden, wird die ursprüngliche verfügbare Untertitelsprache gekennzeichnet zurückgegeben. Bildinhalt, Kameraführung und sichtbare Bedienoberflächen können damit nicht ausgewertet werden.

## Quellen in Antworten

Aktuelle Recherchequellen erscheinen als hervorgehobene Links in der Antwort. Prüfen Sie wichtige Aussagen weiterhin anhand der verlinkten Originalquelle; Suchergebnisse und Webseiten können unvollständig oder fehlerhaft sein.
