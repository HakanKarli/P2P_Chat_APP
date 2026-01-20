import argparse
import json
import logging
import queue
import socket
import struct
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Deque, Dict, List, Optional, Set, Tuple


# ANSI Color Codes
class Colors:
    RESET = "\033[0m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    GRAY = "\033[90m"


@dataclass
class PeerInfo:
    node_key: str
    host: str
    tcp_port: int
    last_seen: float


@dataclass
class Config:
    bind_host: str
    advertise_host: str
    tcp_port: int
    udp_port: int
    discovery_interval: float
    heartbeat_interval: float
    heartbeat_timeout: float
    election_delay: float
    reconnect_base: float
    reconnect_max: float
    log_level: str


def load_config(path: Optional[str]) -> Optional[dict]:
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None


def get_local_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("10.255.255.255", 1))
        ip = sock.getsockname()[0]
        sock.close()
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass

    try:
        host = socket.gethostname()
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_DGRAM)
        for info in infos:
            addr = info[4][0]
            if addr and not addr.startswith("127."):
                return addr
    except OSError:
        pass

    return "127.0.0.1"


def frame_message(payload: dict) -> bytes:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    header = struct.pack("!I", len(data))
    return header + data


class UdpThread(threading.Thread):
    def __init__(self, node: "Node"):
        super().__init__(daemon=True, name="UDP")
        self.node = node
        self.sock = self._setup_socket()

    def _setup_socket(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind((self.node.config.bind_host, self.node.config.udp_port))
        sock.settimeout(0.5)
        return sock

    def run(self):
        self.node.logger.info("UDP listening on %s:%s", self.node.config.bind_host, self.node.config.udp_port)
        
        while not self.node.shutdown.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
                msg = json.loads(data.decode("utf-8"))
                self._handle_message(msg, addr)
            except socket.timeout:
                continue
            except (json.JSONDecodeError, OSError):
                continue

    def _handle_message(self, msg: dict, addr: Tuple[str, int]):
        msg_type = msg.get("type")
        node_key = msg.get("node_key")

        if msg_type == "DISCOVER":
            self.node.send_join(target=addr)
            return

        if node_key is None or node_key == self.node.node_key:
            return

        if msg_type in ("JOIN", "HEARTBEAT"):
            host = msg.get("host")
            tcp_port = msg.get("tcp_port")
            if host and tcp_port:
                self.node.add_peer(node_key, host, tcp_port)

        elif msg_type == "LEAVE":
            self.node.remove_peer(node_key, reason="left")

        elif msg_type == "DEAD":
            self.node.remove_peer(node_key, reason="reported dead")


class TcpListenerThread(threading.Thread):
    def __init__(self, node: "Node"):
        super().__init__(daemon=True, name="TCP-Listener")
        self.node = node
        self.sock = self._setup_socket()

    def _setup_socket(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.node.config.bind_host, self.node.config.tcp_port))
        sock.listen(5)
        sock.settimeout(1.0)
        return sock

    def run(self):
        self.node.logger.info("TCP listening on %s:%s", self.node.config.bind_host, self.node.config.tcp_port)
        
        while not self.node.shutdown.is_set():
            try:
                conn, addr = self.sock.accept()
                handler = TcpHandlerThread(self.node, conn)
                handler.start()
                self.node.threads.append(handler)
            except socket.timeout:
                continue


class TcpHandlerThread(threading.Thread):
    def __init__(self, node: "Node", conn: socket.socket):
        super().__init__(daemon=True, name=f"TCP-Handler")
        self.node = node
        self.conn = conn

    def run(self):
        try:
            while not self.node.shutdown.is_set():
                msg = self._read_frame()
                if msg is None:
                    break
                self._handle_message(msg)
        except (socket.error, ConnectionResetError):
            pass
        finally:
            try:
                self.conn.close()
            except:
                pass

    def _read_frame(self) -> Optional[dict]:
        try:
            header = self.conn.recv(4)
            if len(header) < 4:
                return None
            
            length = struct.unpack("!I", header)[0]
            data = b""
            while len(data) < length:
                chunk = self.conn.recv(min(4096, length - len(data)))
                if not chunk:
                    return None
                data += chunk
            
            return json.loads(data.decode("utf-8"))
        except (socket.error, json.JSONDecodeError):
            return None

    def _handle_message(self, msg: dict):
        msg_type = msg.get("type")

        if msg_type == "HELLO":
            node_key = msg.get("node_key")
            if node_key:
                with self.node.state_lock:
                    self.node.predecessor_key = node_key
                self.node.logger.info("Predecessor connected: %s", node_key)

        elif msg_type == "CHAT":
            self.node.handle_chat(msg)

        elif msg_type == "ELECTION":
            self.node.handle_election(msg)

        elif msg_type == "LEADER":
            self.node.handle_leader(msg)


class HeartbeatThread(threading.Thread):
    def __init__(self, node: "Node"):
        super().__init__(daemon=True, name="Heartbeat")
        self.node = node

    def run(self):
        while not self.node.shutdown.wait(self.node.config.heartbeat_interval):
            self.node.send_heartbeat()


class MonitorThread(threading.Thread):
    def __init__(self, node: "Node"):
        super().__init__(daemon=True, name="Monitor")
        self.node = node

    def run(self):
        while not self.node.shutdown.wait(self.node.config.heartbeat_interval):
            self.node.check_timeouts()


class DiscoveryThread(threading.Thread):
    def __init__(self, node: "Node"):
        super().__init__(daemon=True, name="Discovery")
        self.node = node

    def run(self):
        self.node.send_discover()
        while not self.node.shutdown.wait(self.node.config.discovery_interval):
            self.node.send_discover()


class OutboxFlushThread(threading.Thread):
    """Continuously tries to flush outbox with retry logic"""
    def __init__(self, node: "Node"):
        super().__init__(daemon=True, name="OutboxFlush")
        self.node = node
    
    def run(self):
        while not self.node.shutdown.wait(0.5):  # Check every 500ms
            if self.node.outbox:
                self.node._flush_outbox()


class Node:
    def __init__(self, config: Config):
        self.config = config
        self.node_key = f"{config.advertise_host}:{config.tcp_port}"
        self.logger = logging.getLogger(f"Node-{self.node_key}")

        # Shared state
        self.peers: Dict[str, PeerInfo] = {}
        self.successor_key: Optional[str] = None
        self.predecessor_key: Optional[str] = None
        self.leader_key: Optional[str] = None
        self.seq = 0
        self.in_election = False
        self.seen_messages: Set[str] = set()

        # Message outbox for when successor is unavailable
        self.outbox: Deque[dict] = deque()

        # Chat history
        self.chat_history: List[dict] = []
        self.history_file = f"chat_history_{config.advertise_host.replace('.', '_')}_{config.tcp_port}.json"

        # Sockets
        self.udp_sock: Optional[socket.socket] = None
        self.successor_sock: Optional[socket.socket] = None

        # Locks
        self.state_lock = threading.RLock()
        self.sock_lock = threading.Lock()

        # Control
        self.shutdown = threading.Event()
        self.threads: List[threading.Thread] = []

        # Load history
        self._load_chat_history()

    def start(self):
        # Start all threads
        udp_thread = UdpThread(self)
        tcp_listener = TcpListenerThread(self)
        heartbeat = HeartbeatThread(self)
        monitor = MonitorThread(self)
        discovery = DiscoveryThread(self)

        self.threads.extend([udp_thread, tcp_listener, heartbeat, monitor, discovery])
        self.udp_sock = udp_thread.sock
        
        # Start outbox flush thread
        flush_thread = OutboxFlushThread(self)
        self.threads.append(flush_thread)

        for t in self.threads:
            t.start()

        # Send initial join
        self.send_join()

        # Wait for election delay, then start election
        def delayed_election():
            time.sleep(self.config.election_delay)
            if not self.shutdown.is_set():
                self.start_election()

        election_thread = threading.Thread(target=delayed_election, daemon=True)
        election_thread.start()

    def stop(self):
        self.logger.info("Shutting down...")
        self.send_leave()
        self.shutdown.set()

        # Close sockets
        if self.successor_sock:
            try:
                self.successor_sock.close()
            except:
                pass

        # Wait for threads
        for t in self.threads:
            if t.is_alive():
                t.join(timeout=2.0)

    def send_udp(self, payload: dict, target: Optional[Tuple[str, int]] = None):
        if not self.udp_sock:
            return
        try:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if target:
                self.udp_sock.sendto(data, target)
            else:
                self.udp_sock.sendto(data, ("255.255.255.255", self.config.udp_port))
        except OSError:
            pass

    def send_discover(self):
        self.send_udp({"type": "DISCOVER", "node_key": self.node_key})

    def send_join(self, target: Optional[Tuple[str, int]] = None):
        payload = {
            "type": "JOIN",
            "node_key": self.node_key,
            "host": self.config.advertise_host,
            "tcp_port": self.config.tcp_port,
        }
        self.logger.info("UDP JOIN announce")
        self.send_udp(payload, target)

    def send_leave(self):
        payload = {"type": "LEAVE", "node_key": self.node_key}
        self.logger.info("UDP LEAVE announce")
        self.send_udp(payload)

    def send_dead(self, node_key: str):
        payload = {"type": "DEAD", "node_key": node_key}
        self.logger.warning("UDP DEAD announce: %s", node_key)
        self.send_udp(payload)

    def send_heartbeat(self):
        payload = {
            "type": "HEARTBEAT",
            "node_key": self.node_key,
            "host": self.config.advertise_host,
            "tcp_port": self.config.tcp_port,
        }
        self.send_udp(payload)

    def add_peer(self, node_key: str, host: str, tcp_port: int):
        with self.state_lock:
            self.peers[node_key] = PeerInfo(
                node_key=node_key,
                host=host,
                tcp_port=tcp_port,
                last_seen=time.time(),
            )
        self.update_ring()

    def remove_peer(self, node_key: str, reason: str):
        with self.state_lock:
            if node_key not in self.peers:
                return
            self.logger.info("Peer %s %s", node_key, reason)
            self.peers.pop(node_key, None)
            was_leader = (node_key == self.leader_key)

        self.update_ring()

        if was_leader:
            self.logger.warning("Leader %s, starting re-election", reason)
            self.start_election()

    def check_timeouts(self):
        now = time.time()
        
        with self.state_lock:
            expired = [
                node_key
                for node_key, peer in self.peers.items()
                if now - peer.last_seen > self.config.heartbeat_timeout
            ]

        for node_key in expired:
            self.logger.warning("Peer %s timed out (heartbeat)", node_key)
            self.remove_peer(node_key, "timed out")
            self.send_dead(node_key)

    def update_ring(self):
        with self.state_lock:
            members = sorted([self.node_key] + list(self.peers.keys()))

            if len(members) == 1:
                new_successor = self.node_key
                new_predecessor = self.node_key
            else:
                idx = members.index(self.node_key)
                new_successor = members[(idx + 1) % len(members)]
                new_predecessor = members[(idx - 1) % len(members)]

            if new_successor != self.successor_key:
                self.logger.info("Successor -> %s", new_successor)
                self.successor_key = new_successor
                needs_reconnect = True
            else:
                needs_reconnect = False

            if new_predecessor != self.predecessor_key:
                self.logger.info("Predecessor -> %s", new_predecessor)
                self.predecessor_key = new_predecessor

        if needs_reconnect:
            threading.Thread(target=self.connect_to_successor, daemon=True).start()

    def connect_to_successor(self):
        with self.state_lock:
            successor_key = self.successor_key
            peer = self.peers.get(successor_key) if successor_key else None

        if not peer or successor_key == self.node_key:
            return

        # Close old connection
        with self.sock_lock:
            if self.successor_sock:
                try:
                    self.successor_sock.close()
                except:
                    pass
                self.successor_sock = None

        # Reconnect with exponential backoff
        delay = self.config.reconnect_base
        while not self.shutdown.is_set():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.connect((peer.host, peer.tcp_port))
                
                with self.sock_lock:
                    self.successor_sock = sock

                # Send HELLO
                hello = {"type": "HELLO", "node_key": self.node_key}
                self.send_to_successor(hello)
                
                self.logger.info("Connected to successor %s", successor_key)
                
                # Outbox will be flushed by OutboxFlushThread automatically
                return

            except OSError as exc:
                self.logger.warning("Connect to successor failed: %s", exc)
                time.sleep(delay)
                delay = min(delay * 2, self.config.reconnect_max)

    def send_to_successor(self, payload: dict) -> bool:
        with self.state_lock:
            if self.successor_key == self.node_key:
                return True

        with self.sock_lock:
            if not self.successor_sock:
                # No connection - always buffer
                self.outbox.append(payload)
                self.logger.debug("Buffered message in outbox (no connection)")
                return False

            try:
                frame = frame_message(payload)
                self.successor_sock.sendall(frame)
                return True
            except (OSError, ConnectionResetError) as exc:
                # Send failed - buffer
                self.logger.debug(f"Send failed, buffering: {exc}")
                self.outbox.append(payload)
                # Socket is bad, close it
                try:
                    self.successor_sock.close()
                except:
                    pass
                self.successor_sock = None
                # Trigger reconnect in background
                threading.Thread(target=self.connect_to_successor, daemon=True).start()
                return False
    
    def _flush_outbox(self):
        """Flush buffered messages to successor (with retry logic)."""
        with self.sock_lock:
            if not self.successor_sock:
                return
            
            flushed = 0
            failed = False
            
            # Try to send all messages in outbox
            while self.outbox and not failed:
                payload = self.outbox[0]  # Peek first
                try:
                    frame = frame_message(payload)
                    self.successor_sock.sendall(frame)
                    self.outbox.popleft()  # Remove only if successful
                    flushed += 1
                except (OSError, ConnectionResetError) as exc:
                    # Failed - stop trying, connection is bad
                    self.logger.debug(f"Flush failed: {exc}")
                    try:
                        self.successor_sock.close()
                    except:
                        pass
                    self.successor_sock = None
                    failed = True
            
            if flushed > 0:
                self.logger.info(f"Flushed {flushed} messages from outbox")

    def handle_chat(self, msg: dict):
        msg_id = msg.get("msg_id")
        
        with self.state_lock:
            if msg_id and msg_id in self.seen_messages:
                return
            if msg_id:
                self.seen_messages.add(msg_id)
                if len(self.seen_messages) > 2000:
                    self.seen_messages = set(list(self.seen_messages)[-1000:])

        origin = msg.get("origin")
        seq = msg.get("seq")
        text = msg.get("text")
        timestamp = msg.get("timestamp", time.time())

        if text is not None:
            self.logger.info("CHAT recv msg_id=%s origin=%s seq=%s", msg_id, origin, seq)
            self._save_chat_message(origin, seq, text, timestamp, is_own=False)
            self._print_chat_message(origin, seq, text, timestamp, is_own=False)

        self.send_to_successor(msg)

    def handle_election(self, msg: dict):
        candidate_key = msg.get("candidate_key")
        if not candidate_key:
            return

        self.logger.info("Election message: candidate=%s", candidate_key)

        if candidate_key == self.node_key:
            with self.state_lock:
                self.leader_key = self.node_key
                self.in_election = False
            
            self.logger.info("Elected leader: %s", self.leader_key)
            self.send_to_successor({
                "type": "LEADER",
                "leader_key": self.node_key,
                "origin": self.node_key,
            })

        elif candidate_key < self.node_key:
            self.logger.info("Replacing candidate with self: %s", self.node_key)
            self.send_to_successor({
                "type": "ELECTION",
                "candidate_key": self.node_key,
            })

        else:
            self.send_to_successor(msg)

    def handle_leader(self, msg: dict):
        leader_key = msg.get("leader_key")
        origin = msg.get("origin")

        with self.state_lock:
            if leader_key:
                self.leader_key = leader_key
            self.in_election = False

        self.logger.info("Leader announcement: %s", leader_key)

        if origin != self.node_key:
            self.send_to_successor(msg)

    def start_election(self):
        with self.state_lock:
            if self.in_election:
                return
            self.in_election = True

        self.logger.info("Election started")
        self.send_to_successor({
            "type": "ELECTION",
            "candidate_key": self.node_key,
        })

    def send_chat(self, text: str):
        with self.state_lock:
            self.seq += 1
            seq = self.seq

        msg_id = f"{self.node_key}-{seq}"
        timestamp = time.time()

        payload = {
            "type": "CHAT",
            "origin": self.node_key,
            "seq": seq,
            "msg_id": msg_id,
            "text": text,
            "timestamp": timestamp,
        }

        with self.state_lock:
            self.seen_messages.add(msg_id)

        self.logger.info("CHAT send msg_id=%s", msg_id)
        self._save_chat_message(self.node_key, seq, text, timestamp, is_own=True)
        self._print_chat_message(self.node_key, seq, text, timestamp, is_own=True)
        self.send_to_successor(payload)

    def _load_chat_history(self):
        if Path(self.history_file).exists():
            try:
                with open(self.history_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.chat_history = data.get("messages", [])
                    self.seq = data.get("last_seq", 0)
                    self.logger.info(f"Loaded {len(self.chat_history)} messages from history")
                    
                    if self.chat_history:
                        print(f"\n{Colors.GRAY}=== Chat History Restored ==={Colors.RESET}")
                        for msg in self.chat_history[-20:]:
                            origin = msg.get("origin", "unknown")
                            seq = msg.get("seq", 0)
                            text = msg.get("text", "")
                            timestamp = msg.get("timestamp", 0)
                            is_own = msg.get("is_own", False)
                            self._print_chat_message(origin, seq, text, timestamp, is_own)
                        print(f"{Colors.GRAY}============================={Colors.RESET}\n")
            except (json.JSONDecodeError, OSError) as e:
                self.logger.warning(f"Failed to load chat history: {e}")

    def _save_chat_history(self):
        try:
            data = {
                "node_key": self.node_key,
                "last_seq": self.seq,
                "messages": self.chat_history,
            }
            with open(self.history_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except OSError as e:
            self.logger.warning(f"Failed to save chat history: {e}")

    def _save_chat_message(self, origin: str, seq: int, text: str, timestamp: float, is_own: bool):
        msg = {
            "origin": origin,
            "seq": seq,
            "text": text,
            "timestamp": timestamp,
            "is_own": is_own,
        }
        self.chat_history.append(msg)
        if len(self.chat_history) > 1000:
            self.chat_history = self.chat_history[-1000:]
        self._save_chat_history()

    def _print_chat_message(self, origin: str, seq: int, text: str, timestamp: float, is_own: bool):
        dt = datetime.fromtimestamp(timestamp)
        time_str = dt.strftime("%H:%M:%S")

        if is_own:
            print(f"{Colors.GRAY}[{time_str}]{Colors.RESET} {Colors.GREEN}[me#{seq}]{Colors.RESET} {text}")
        else:
            print(f"{Colors.GRAY}[{time_str}]{Colors.RESET} {Colors.CYAN}[{origin}#{seq}]{Colors.RESET} {text}")

    def print_help(self):
        print("Commands:")
        print("  peers       - list known peers")
        print("  leader      - show leader")
        print("  links       - show successor/predecessor")
        print("  status      - summary")
        print("  send <msg>  - send chat message")
        print("  quit/exit   - shutdown")

    def print_peers(self):
        with self.state_lock:
            peers_copy = list(self.peers.values())

        print("Peers:")
        for peer in sorted(peers_copy, key=lambda p: p.node_key):
            age = time.time() - peer.last_seen
            print(f"  {peer.node_key} (last {age:.1f}s)")

    def print_status(self):
        with self.state_lock:
            print(f"Node: {self.node_key}")
            print(f"Leader: {self.leader_key}")
            print(f"Successor: {self.successor_key}")
            print(f"Predecessor: {self.predecessor_key}")
            print(f"Known peers: {len(self.peers)}")


def build_config(args: argparse.Namespace, file_cfg: Optional[dict]) -> Config:
    cfg = file_cfg or {}
    bind_host = args.bind_host or cfg.get("bind_host", "0.0.0.0")
    advertise_host = get_local_ip()

    return Config(
        bind_host=bind_host,
        advertise_host=advertise_host,
        tcp_port=int(args.tcp_port or cfg.get("tcp_port", 9000)),
        udp_port=int(args.udp_port or cfg.get("udp_port", 9999)),
        discovery_interval=float(args.discovery_interval or cfg.get("discovery_interval", 10.0)),
        heartbeat_interval=float(args.heartbeat_interval or cfg.get("heartbeat_interval", 2.0)),
        heartbeat_timeout=float(args.heartbeat_timeout or cfg.get("heartbeat_timeout", 6.0)),
        election_delay=float(args.election_delay or cfg.get("election_delay", 5.0)),
        reconnect_base=float(args.reconnect_base or cfg.get("reconnect_base", 1.0)),
        reconnect_max=float(args.reconnect_max or cfg.get("reconnect_max", 10.0)),
        log_level=args.log_level or cfg.get("log_level", "INFO"),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="P2P Ring Chat (LAN) - Threading Version")
    parser.add_argument("--config", help="Path to config JSON")
    parser.add_argument("--bind-host", help="Bind address")
    parser.add_argument("--tcp-port", type=int, help="TCP port")
    parser.add_argument("--udp-port", type=int, help="UDP port")
    parser.add_argument("--discovery-interval", type=float, help="Discovery interval")
    parser.add_argument("--heartbeat-interval", type=float, help="Heartbeat interval")
    parser.add_argument("--heartbeat-timeout", type=float, help="Heartbeat timeout")
    parser.add_argument("--election-delay", type=float, help="Election delay")
    parser.add_argument("--reconnect-base", type=float, help="Reconnect base")
    parser.add_argument("--reconnect-max", type=float, help="Reconnect max")
    parser.add_argument("--log-level", help="Log level")
    return parser.parse_args()


def main():
    args = parse_args()
    file_cfg = load_config(args.config)
    config = build_config(args, file_cfg)

    log_level = getattr(logging, config.log_level.upper(), logging.INFO)
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    log_file = f"p2p_ring_{config.advertise_host.replace('.', '_')}_{config.tcp_port}.log"

    handlers = [logging.FileHandler(log_file, encoding="utf-8")]
    logging.basicConfig(level=log_level, format=log_format, handlers=handlers)

    node = Node(config)
    logging.getLogger("startup").info("Advertise IP: %s", config.advertise_host)

    print(f"Logs: {log_file}")
    print(f"Node: {config.advertise_host}:{config.tcp_port}")
    print("Type 'help' for commands\n")

    node.start()

    # Input loop in main thread
    try:
        while not node.shutdown.is_set():
            try:
                line = input().strip()
            except EOFError:
                break

            if not line:
                continue

            if line in ("quit", "exit"):
                node.stop()
                break

            if line == "help":
                node.print_help()
            elif line == "peers":
                node.print_peers()
            elif line == "leader":
                print(f"Leader: {node.leader_key}")
            elif line == "links":
                print(f"Successor: {node.successor_key} | Predecessor: {node.predecessor_key}")
            elif line == "status":
                node.print_status()
            elif line.startswith("send "):
                text = line[5:].strip()
                if text:
                    node.send_chat(text)
            else:
                print("Unknown command. Type 'help'.")

    except KeyboardInterrupt:
        node.stop()


if __name__ == "__main__":
    main()
