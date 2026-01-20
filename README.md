# P2P Ring Chat (LAN)

Einfache, stabile P2P-Chatapplikation im lokalen Netzwerk mit Ring-Topologie.

## Überblick
- **Discovery** via UDP Broadcast (DISCOVER/JOIN).
- **Ring** über TCP: jeder Node hält eine Verbindung zum Successor; der Predecessor verbindet sich inbound.
- **Heartbeats** via UDP (HEARTBEAT), geplanter Exit via LEAVE.
- **Leaderwahl** mittels LCR-Algorithmus (verzögert startbar).
- **Nachrichten** mit 4-Byte Length-Header; Chatnachrichten enthalten Sequenznummern.
- **FIFO-Queue** für Forwarding, falls Successor temporär nicht erreichbar ist.
- **CLI** mit Statusanzeigen (Peer-Liste, Leader, Links, Verbindungsstatus).

## Start

### 1) Beispiel-Konfiguration
Siehe [config.example.json](config.example.json).

### 2) Node starten
```bash
python p2p_ring_chat.py --config config.example.json
```

Oder per CLI-Argumente:
```bash
python p2p_ring_chat.py --tcp-port 9001
```

## CLI-Kommandos
- `peers` – bekannte Peers
- `leader` – aktueller Leader
- `links` – Successor/Predecessor
- `status` – Kurzstatus
- `send <msg>` – Chatnachricht senden
- `quit` / `exit` – sauberer Shutdown

## Hinweise
- Ring-Ordnung ist deterministisch nach `IP:Port`.
- Die lokale IP wird automatisch für die Peer-Ankündigung verwendet.
