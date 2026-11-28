---
title: Drucker anschließen
description: Welche Drucker Labelyard ansteuert, was das Netz erlauben muss und wie du einen Drucker in der Weboberfläche hinzufügst.
sidebar:
  label: Drucker und Hardware
---

Labelyard erreicht jeden Drucker über das Netz. Einen P-touch, der nur USB kann, erreicht der Hub über eine kleine Netzwerkbrücke, siehe [USB-Drucker über eine Netzwerkbrücke](#usb-drucker-über-eine-netzwerkbrücke). Drucker fügst du in der Weboberfläche unter **Einstellungen > Drucker** hinzu, im laufenden Betrieb und ohne Neustart; Druckervariablen gibt es nicht.

## Banddrucker

Drucker der Brother PT-Serie und QL-Serie im Netz, zum Beispiel der PT-P750W und der QL-820NWB. Der Hub druckt über TCP-Port 9100 und liest den Zustand des Druckers, auch das eingelegte Band oder die Etikettenrolle, über SNMP (UDP-Port 161).

Die Adresse eines Druckers findest du in den DHCP-Zuteilungen deines Routers, im Display oder auf der Webseite des Druckers, oder du suchst dein Netz nach den beiden Ports ab:

```bash
nmap -p 9100,161 192.0.2.0/24 --open
```

Prüf vom Host aus, auf dem der Hub läuft, bevor du den Drucker hinzufügst:

```bash
# Drucken
nc -zv <druckeradresse> 9100
# Zustand; die Antwort nennt Hersteller und Modell
snmpwalk -v 2c -c public <druckeradresse> 1.3.6.1.4.1.2435.2.3.9.1.1.7.0
```

Antwortet SNMP nicht, öffne die Webseite des Druckers und schalte SNMPv1/v2c mit der Community `public` ein (bei vielen Modellen unter **Network > Protocols > SNMP**).

### Statuserkennung

Der Hub liest Zustand und Band eines Banddruckers nur dann über SNMP, wenn **Statuserkennung** für diesen Drucker eingeschaltet ist. Schalte sie im Druckerformular unter **Einstellungen > Drucker** ein und gib die **SNMP-Community** ein (`public`, solange du sie am Drucker nicht geändert hast). Der Hub speichert die Community und zeigt sie nie wieder an: Das Feld bleibt leer, und ein Hinweis sagt, dass eine gespeichert ist. Lass das Feld leer, um sie zu behalten, oder gib eine neue ein, um sie zu ersetzen. Änderst du Host oder Port, gib die Community erneut ein; der Hub schickt eine gespeicherte Community nicht an eine andere Adresse. Drucken funktioniert auch ohne Statuserkennung; die Übersicht zeigt den Drucker dann als nicht überwacht.

## USB-Drucker über eine Netzwerkbrücke

Einen P-touch, der nur USB hat, etwa den PT-D600, steckst du an einen kleinen Linux-Rechner daneben, zum Beispiel einen Raspberry Pi. Die Brücke darauf bietet den Drucker auf TCP-Port 9100 an und beantwortet die SNMP-Fragen des Hubs mit dem Zustand, den der Drucker selbst meldet. Der Hub behandelt ihn dadurch wie einen Netzwerkdrucker, mit Statuserkennung und Etikettenzähler. Die Brücke ist ein einzelnes Python-Skript ohne Abhängigkeiten und liegt im Repository unter [`examples/usb-bridge/`](https://github.com/strausmann/labelyard/tree/main/examples/usb-bridge). Getestet ist sie mit dem PT-D600; andere P-touch-Modelle mit USB laufen wahrscheinlich auch. Sag uns gern Bescheid, mit einem [Kompatibilitätsbericht](https://github.com/strausmann/labelyard/issues/new?template=printer-compatibility.yml).

Der Rechner braucht Linux mit Python 3.10 oder neuer. Raspberry Pi OS bringt beides mit, und sein Kernelmodul `usblp` macht den Drucker zu `/dev/usb/lp0`.

1. Steck den Drucker an, schalte ihn ein und prüf, ob er da ist:

   ```bash
   ls -l /dev/usb/lp0
   ```

2. Kopier `ptouch_bridge.py` und `ptouch-bridge.service` auf den Rechner und starte die Brücke. Sie läuft ohne Root-Rechte: Der Drucker gehört der Gruppe `lp`, und der Dienst bekommt nur das Recht, Port 161 zu nutzen.

   ```bash
   sudo install -m 755 ptouch_bridge.py /usr/local/bin/ptouch-bridge
   sudo install -m 644 ptouch-bridge.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now ptouch-bridge
   ```

3. Füg den Drucker unter **Einstellungen > Drucker** wie einen Netzwerkdrucker hinzu: die Adresse des Rechners als Host, Port `9100`, Modell `PT-D600`, **Statuserkennung** an mit der Community `public`. Die Prüfungen oben funktionieren genauso; die SNMP-Antwort nennt das Modell, das die Brücke am USB gefunden hat.
4. Mach einen Testdruck.

Die Einstellungen der Brücke stehen in der Dienstdatei: `PTB_DEVICE` (das Druckergerät), `PTB_COMMUNITY` (die SNMP-Community), `PTB_NET_IF` (die Netzwerkschnittstelle, deren Adresse die Brücke meldet) und `PTB_LABEL_COUNTER`. Der Etikettenzähler zählt die Etiketten, die der Drucker als gedruckt meldet. Mit `auto` bietet die Brücke ihn an, sobald der Drucker sein erstes Etikett gemeldet hat: Der Testdruck zählt dann als gedruckt, und jeden späteren Auftrag bestätigt der Zähler. `on` bietet ihn sofort an, `off` nie. `journalctl -u ptouch-bridge -f` zeigt jeden Auftrag mit seinen Seiten und das, was der Drucker meldet.

Gut zu wissen:

- Die Brücke hat keine Anmeldung: Wer Port 9100 erreicht, kann drucken. Lass nur den Host des Hubs an die Ports 9100 und 161, zum Beispiel mit einer Firewall auf der Brücke.
- Der PT-D600 hat nur ein Vollschnitt-Messer. Der Hub schneidet seine Etiketten ganz durch, auch wenn ein Halbschnitt verlangt ist.
- Der PT-D600 merkt nicht, wenn eine Bandkassette leer ist: Etiketten auf einer leeren Kassette zählen als gedruckt.
- Eine Verbindung auf einmal. Die Warteschlange des Hubs schickt die Aufträge ohnehin nacheinander.

## Seitendrucker

Bürodrucker, die IPP sprechen, bedrucken Etikettenbögen. Füg sie als **Seitendrucker (IPP)** mit ihrer IPP-Adresse hinzu, zum Beispiel `ipp://printer.example.invalid:631/ipp/print`. Lies den Drucker vor dem Speichern einmal aus, damit der Hub seine Fächer kennt; dann schaltest du die Fächer ein, die du nutzt, und wählst ein Standardfach. Wie es weitergeht, steht unter [Bögen und Layouts](/de/using/sheets/).

## Nach dem Hinzufügen

- **Testdruck.** Jeder Drucker hat einen Testdruck, damit du sofort siehst, dass Adresse und Protokoll stimmen. Das Testetikett zeigt den Namen des Druckers, die Bandbreite, das Datum und ein 50-mm-Lineal – auf jedem Band, das der Drucker nimmt; ab 29 mm trägt es zusätzlich einen QR-Code zur Seite des Druckers.
- **Seitendrucker kalibrieren.** Ein Seitendrucker druckt selten genau dorthin, wo die Seite es vorgibt. Druck die Kalibrierseite, miss, wie weit er daneben liegt, und der Hub verschiebt jeden Druck aus diesem Fach um das Gegenteil. [Seitendrucker kalibrieren](/de/using/calibration/) führt dich Schritt für Schritt durch.
- **Den Zustand im Blick behalten.** Die Seite des Druckers zeigt seinen Zustand live: bereit, druckt, Deckel offen, Material leer.

Der Container muss das Netz der Drucker erreichen. Mit dem Standard-Bridge-Netz klappt das, solange der Host es erreicht; sonst prüf Routing und Firewalls zwischen Docker-Host und Druckern.
