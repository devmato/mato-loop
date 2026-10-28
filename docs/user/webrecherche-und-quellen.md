# Webrecherche und Quellen

## Websuchmodus der Session

| Modus | Verhalten |
| --- | --- |
| Erzwingen | Vor der normalen Antwort muss eine erfolgreiche Websuche stattfinden |
| Verfügbar | Der Agent kann bei Bedarf suchen |
| Deaktiviert | Das Suchwerkzeug wird nicht angeboten |

Die Auswahl des tatsächlichen Suchproviders erfolgt durch MatoLoop und kann vom Modell nicht überschrieben werden.

## Suchprovider

Ohne weitere Einrichtung verwendet MatoLoop eine eingebaute Suche ohne API-Schlüssel. Zusätzliche SearXNG-Instanzen sowie Primär- und optionaler Fallback-Provider werden unter **Einstellungen > Integrationen** verwaltet.

Der Fallback wird nur bei vorübergehenden Fehlern des primären Providers verwendet.

## Webseiten abrufen

`webfetch` ruft eine konkrete HTTP- oder HTTPS-Adresse ab und ist unabhängig von der Websuche konfiguriert. Das ausgewählte Agentenprofil legt dafür getrennt für private und öffentliche Modelle eine Richtlinie fest:

- **Deaktiviert**
- **Nachfragen**
- **Automatisch**

Administratorinnen und Administratoren konfigurieren die Verfügbarkeit unter **Tools > Tool-Einstellungen**. Die gemeinsame Einstellung **Nur öffentliche Zieladressen zulassen** steht unter **Tools > Einstellungen > Webabrufe und Downloads** und ist standardmäßig aktiv. Webfetch verwendet HTTP mit einem browserähnlichen Verbindungsprofil, führt aber kein JavaScript aus. Jede Weiterleitung wird erneut autorisiert und ihre Zieladresse geprüft. Das Abschalten der Adressbeschränkung erlaubt auch private vom Backend erreichbare Ziele. Große Ergebnisse bleiben vollständig gespeichert; fehlende Abschnitte können über den Ergebnisverweis ohne erneuten Abruf gelesen werden. Browsersteuerung dient nötiger Seiteninteraktion, nicht als automatische Antwort auf jede unbrauchbare Quelle.

Für Bildreferenzen kann der Agent Bildsuche verwenden und muss die konkrete Bildadresse vor Verwendung abrufen. Ein leerer Webfetch-Inhalt gilt als kontrollierter Fehler; ein identischer Wiederholungsversuch ist erlaubt, ohne automatische Browserübernahme. Originaldokumente können ausdrücklich als Datei in der Ablage heruntergeladen werden.

## Veröffentlichte Untertitel

`fetch_transcript` ruft vorhandene veröffentlichte Untertitel unterstützter öffentlicher YouTube-Videos ab und verwendet dieselbe Webzugriffsrichtlinie. Das Werkzeug lädt kein Video herunter, übersetzt keine Untertitel und startet keine automatische Transkription. Ist eine gewünschte Sprache nicht vorhanden, wird die ursprüngliche verfügbare Untertitelsprache gekennzeichnet zurückgegeben. Bildinhalt, Kameraführung und sichtbare Bedienoberflächen können damit nicht ausgewertet werden.

## Quellen in Antworten

Aktuelle Recherchequellen erscheinen als hervorgehobene Links in der Antwort. Prüfen Sie wichtige Aussagen weiterhin anhand der verlinkten Originalquelle; Suchergebnisse und Webseiten können unvollständig oder fehlerhaft sein.
