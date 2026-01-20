# Resilient P2P Ring Chat System

A decentralized P2P chat app using a self-healing ring topology. Features automatic UDP discovery, LCR leader election, and fault tolerance via heartbeats. Includes reliable TCP messaging with an outbox retry mechanism.

## Features

*   **Decentralized**: No central server.
*   **Auto-Discovery**: Zero-configuration via UDP broadcasts.
*   **Self-Healing**: Automatically detects and repairs node failures.
*   **Reliable Messaging**: TCP-based ring with FIFO ordering and message buffering (Outbox).
*   **Leader Election**: Uses LCR algorithm.
*   **Multithreaded**: Non-blocking UI and network handling.

## Architecture

*   **TCP**: Persistent ring connections (Chat, Election).
*   **UDP**: Discovery and Heartbeats.

## Usage

```bash
# Run a node
python p2p_ring_chat.py --tcp-port 9000 --udp-port 9999
```

**Commands:**
*   `peers`, `links`, `leader`, `status`: View network state.
*   `quit`: Shutdown.
*   Type text to chat.

