import socket
import os
import time
import datetime
from enum import Enum
from dataclasses import dataclass
from dataclasses_json import dataclass_json
import argparse
from threading import Thread
from queue import Queue
import json
import logging

# DONE Implement server-side client health checks
# DONE Implement ACK for message receipt, update receipt, etc.
# DONE Implement client message send ACK check -> server disable client
# DONE Implement offline messages
# DONE implement message hash to use for ACK so that we only ACK a specific message
# DONE client de-reg retry 5 times 500msec each
# DONE error if offline message sent when recipient is actually online
# DONE add timestamp to offline message .data property
# DONE add offline data structure to ClientInstance dataclass
# TODO clean up message formatting to match assignment examples
# DONE don't allow multiple regs for a single client (reg must dereg first)
# TODO server and client shutdowns


MSG_SIZE = 4096
TIMEOUT_DEREG = 500
TIMEOUT_MESSAGE = 500


class Events(Enum):
    REGISTER = 1
    DEREGISTER = 2
    REGISTER_CONFIRM = 3
    CLIENT_UPDATE = 4

    MESSAGE = 10
    DIRECT_MESSAGE = 11
    OFFLINE_MESSAGE = 12

    PING = 97
    ERROR = 98
    ACK = 99


@dataclass_json
@dataclass
class Message:
    event_id: Events
    nickname: str
    msg_hash: int = None
    data: str = None
    recipient: str = None

    def __str__(self):
        return f'{self.event_id}|{self.nickname}|{self.recipient}|{self.data}'

    def __hash__(self):
        return hash((self.event_id, self.nickname, self.data, self.recipient))


@dataclass_json
@dataclass
class ClientInstance:
    nickname: str
    ip: str
    port: int
    online: bool
    offline_messages = []

    def __str__(self):
        status = 'ONLINE' if self.online else 'OFFLINE'
        return f'{self.nickname} @ {self.ip}:{self.port} is {status}'

    def __eq__(self, other) -> bool:
        if self.nickname == other.nickname:
            return True
        return False

# Server Section


class Server:

    def __init__(self, port, logger):
        self.ip = socket.gethostbyname(socket.gethostname())
        self.port = port
        self.nickname = 'SERVER'
        self.server_persona = ClientInstance(
            nickname=self.nickname, ip=self.ip, port=self.port, online=True)
        self.clients = [ self.server_persona ]
        self.inputBuffer = Queue()
        self.ack_checker = {}
        self.logger = logger
        self.server_thread()

    def is_client(self, nickname):
        for client in self.clients:
            if client.nickname == nickname:
                self.logger.debug(f'Found client {nickname} = {client}')
                return True
        self.logger.debug(f'Failed to find client {nickname}')
        return False

    def get_client(self, nickname):
        if nickname == self.nickname:
            self.logger.debug(f'Matched SERVER')
            return self.server_persona
        for client in self.clients:
            if client.nickname == nickname:
                self.logger.debug(f'Matched client {nickname} = {client}')
                return client

    def disable_client(self, client):
        client.online = False
        self.logger.debug(f'DISABLED: {client}')
        self.update_clients()

    def enable_client(self, client):
        client.online = True
        self.logger.debug(f'ENABLED: {client}')
        self.update_clients()

    def register_client(self, addr, msg):
        self.logger.debug(f'REGISTER REQUEST: {addr}->{msg.nickname}')
        if self.is_client(msg.nickname):
            client = self.get_client(msg.nickname)
            client.ip = addr[0]
            client.port = int(addr[1])
            client.online = True
            self.logger.info(
                f'CLIENT UPDATED: {msg.nickname} to {addr[0]}:{addr[1]}')
        else:
            self.logger.info(f'CLIENT REGISTERED: {msg.nickname} at {addr[0]}:{addr[1]}')
            client = ClientInstance(ip=addr[0], port=int(
                addr[1]), nickname=msg.nickname, online=True)
            self.clients.append(client)
            self.logger.info(
                f'CLIENT REGISTERED: {msg.nickname} to {addr[0]}:{addr[1]}')
        self.send_ack(msg)
        self.direct_message(Message(event_id=Events.REGISTER_CONFIRM, nickname=self.nickname,
                                    recipient=client.nickname, data='[[ Welcome, you are registered. ]]'))
        self.update_clients()
        if len(client.offline_messages) > 0:
            self.direct_message(Message(event_id=Events.DIRECT_MESSAGE, nickname=self.nickname,
                                    recipient=client.nickname, data='[[ You have messages! ]]'))
            for message in client.offline_messages:
                self.direct_message(Message(event_id=Events.DIRECT_MESSAGE, nickname=message.nickname,
                                    recipient=message.recipient, data=message.data))
            client.offline_messages = []

    def deregister_client(self, msg):
        client = self.get_client(msg.nickname)
        self.logger.info(f'DISABLE REQUEST: {client.nickname}')
        self.send_ack(msg)
        self.disable_client(client)
        self.update_clients()

    def update_clients(self):
        msg = Message(event_id=Events.CLIENT_UPDATE, nickname=self.nickname,
                      data=json.dumps([client.to_json() for client in self.clients]))
        self.broadcast(msg, self.server_persona)
        self.logger.debug('SERVER: UPDATE CLIENTS')

    def check_ack(self, msg):
        if msg.data in self.ack_checker:
            return True
        return False

    def track_ack(self, msg):
        self.logger.debug(f'TRACK: hash {msg.msg_hash} for {msg}')
        self.ack_checker[msg.msg_hash] = False

    def check_ack_timeout(self, msg, timeout, retries):
        intervals = timeout // 10
        sleep_time = timeout / intervals
        for _ in range(retries+1):
            for _ in range(intervals):
                if self.check_ack(msg):
                    return True
                time.sleep(sleep_time/1000)
        return False

    def receive_ack(self, msg):
        if self.check_ack(msg):
            self.ack_checker[msg.data] = True
            self.logger.debug(
                f'ACK: VALID to {self.nickname} from {msg.nickname} with hash {msg.data}')
        else:
            self.logger.debug(
                f'ACK: INVALID to {self.nickname} from {msg.nickname} with hash {msg.data}')

    def send_ack(self, msg):
        ack_msg = Message(event_id=Events.ACK, nickname=self.nickname,
                          data=msg.msg_hash, recipient=msg.nickname)
        self.direct_message(ack_msg)

    def check_client(self, target):
        ping = Message(event_id=Events.PING, nickname=self.nickname,
                                    recipient=target.nickname, data=f'PING!')
        ping.msg_hash = hash(ping)
        self.track_ack(ping)
        self.direct_message(ping)
        if not self.check_ack_timeout(ping, TIMEOUT_MESSAGE, 5):
            target.online = False
            self.update_clients()
            self.logger.debug(f'[No PING-ack from {target}]')

    def store_offline(self, msg):
        if self.is_client(msg.recipient) and msg.recipient != self.nickname:
            target = self.get_client(msg.recipient)
            self.check_client(target)
            if target.online:
                self.direct_message(Message(event_id=Events.ERROR, nickname=self.nickname,
                                    recipient=msg.nickname, data=f'ERROR: {msg.recipient} IS online!'))
                self.update_clients()
            else:
                target.offline_messages.append(msg)
                self.send_ack(msg)
                self.direct_message(Message(event_id=Events.DIRECT_MESSAGE, nickname=self.nickname,
                                     recipient=msg.nickname, data='[[ Message received by the server and saved ]]'))
        else:
            self.direct_message(Message(event_id=Events.ERROR, nickname=self.nickname,
                                recipient=msg.nickname, data=f'ERROR: {msg.recipient} does not exist!'))
            self.update_clients()

    def direct_message(self, message):
        if self.is_client(message.recipient):
            message.msg_hash = hash(message)
            target_client = self.get_client(message.recipient)
            self.logger.debug(
                f'SERVER SEND: {message} to {target_client}')
            try:
                self.server.sendto(message.to_json().encode(),
                                   (target_client.ip, target_client.port))
                self.track_ack(message)
            except Exception as e:
                self.logger.debug(
                    f'FAILED: send to: {target_client} -> {e} -- DISABLING {target_client}')
                self.disable_client(target_client)
        else:
            self.logger.debug(f'UNKNOWN: {message.recipient}')

    def broadcast(self, message, sending_client):
        self.logger.debug(f'BROADCAST: {message} from {sending_client}')
        for client in self.clients:
            if client != sending_client and client.online:
                try:
                    self.logger.debug(f'SENDING: {client}')
                    self.server.sendto(
                        message.to_json().encode(), (client.ip, client.port))

                except Exception as e:
                    self.logger.info(
                        f'FAILED: send to: {client} -> {e} -- DISABLING {client}')
                    self.disable_client(client)
    
    def server_thread(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.ip = '127.0.0.1'
        self.server.bind((self.ip, self.port))
        self.logger.info(f'SERVER: {self.ip}:{self.port}')

        Thread(target=self.receiver_thread, args=()).start()

        while True:
            while not self.inputBuffer.empty():
                data, addr = self.inputBuffer.get()
                self.logger.debug(f'RECEIVED: {addr}: {data.decode()}')
                self.handle_message(data, addr)

    def receiver_thread(self):
        self.logger.info(f'RECEIVER_THREAD: {self.ip}:{self.port}')
        while True:
            data, addr = self.server.recvfrom(MSG_SIZE)
            self.inputBuffer.put((data, addr))

    def handle_message(self, data, addr):
        msg = Message.from_json(data)
        self.logger.debug(f'HANDLE: {addr} -> {msg.data}')

        if msg.event_id == Events.REGISTER:
            self.register_client(addr, msg)

        if msg.event_id == Events.DEREGISTER:
            self.deregister_client(msg)

        if msg.event_id == Events.ACK:
            self.receive_ack(msg)

        if msg.event_id == Events.MESSAGE:
            client = self.get_client(msg.nickname)
            if client is None:
                self.logger.info('ERROR: unknown sender - dropping message!')
            else:
                self.logger.debug(f'BROADCAST: {client} {msg.data}')
                self.broadcast(msg, client)

        if msg.event_id == Events.OFFLINE_MESSAGE:
            self.logger.debug(
                f'OFFLINE MESSAGE: {msg.nickname} -> {msg.recipient}: {msg.data}')
            self.store_offline(msg)

# Client Section
class Client:

    def __init__(self, name, client_port, ip, port, logger):
        self.server_ip = ip
        self.server_port = port
        self.client_port = client_port
        self.client_ip = socket.gethostbyname(socket.gethostname())
        self.online = True
        self.nickname = name
        self.peers = [ClientInstance(
            nickname='SERVER', ip=self.server_ip, port=self.server_port, online=True)]
        self.logger = logger
        self.ack_checker = {}
        self.client_thread()

    def show_peers(self):
        for peer in self.peers:
            print(peer)

    def is_peer(self, nickname):
        for peer in self.peers:
            if peer.nickname == nickname:
                return True
        return False

    def get_peer(self, nickname):
        for peer in self.peers:
            if peer.nickname == nickname:
                return peer

    def track_ack(self, msg):
        self.logger.debug(f'TRACK: hash {msg.msg_hash} for {msg}')
        self.ack_checker[msg.msg_hash] = False

    def check_ack(self, msg):
        self.logger.debug(
            f'ACK: CHECKING from {msg.nickname} with hash {msg.msg_hash}')
        if msg.msg_hash in self.ack_checker:
            return self.ack_checker[msg.msg_hash]
        return False

    def handle_ack(self, msg):
        self.logger.debug(f'HANDLING ACK: {msg.data}')
        if msg.data in self.ack_checker:
            self.ack_checker[msg.data] = True
            self.logger.debug(
                f'ACK: VALID from {msg.nickname} with hash {msg.data}')
        else:
            self.logger.debug(
                f'ACK: MISSING from {msg.nickname} with hash {msg.data}')

    def check_ack_timeout(self, msg, timeout, retries):
        intervals = timeout // 10
        sleep_time = timeout / intervals
        for _ in range(retries+1):
            for _ in range(intervals):
                if self.check_ack(msg):
                    return True
                time.sleep(sleep_time/1000)
        return False

    def send_ack(self, msg):
        ack_msg = Message(event_id=Events.ACK, nickname=self.nickname,
                          data=msg.msg_hash, recipient=msg.nickname)
        self.direct_message(ack_msg)

    def register(self, nickname):
        if self.nickname != nickname:
            self.deregister(self.nickname)
        self.nickname = nickname
        reg_msg = Message(event_id=Events.REGISTER,
                          nickname=self.nickname, recipient='SERVER')
        reg_msg.msg_hash = hash(reg_msg)
        self.logger.debug(f'REGISTERING: as {self.nickname} using {reg_msg}')
        self.track_ack(reg_msg)
        self.direct_message(reg_msg)
        self.online = True

    def deregister(self, nickname):
        reg_msg = Message(event_id=Events.DEREGISTER, nickname=nickname, recipient='SERVER')
        reg_msg.msg_hash = hash(reg_msg)
        self.logger.debug(
            f'DE-REGISTERING: as {self.nickname} using {reg_msg}')
        self.track_ack(reg_msg)
        self.direct_message(reg_msg)
        if not self.check_ack_timeout(reg_msg, TIMEOUT_DEREG, 5):
            self.logger.debug(
                f'TIMEOUT: de-register {nickname} hash {hash(reg_msg)}')
            self.logger.debug(f'CLIENT EXIT: {nickname}')
            print("[Server not responding]")
            print("[Exiting]")
            os._exit(1)
        self.online = False
        print('[[ You are offline. Bye! ]]')

    def update_clients(self, msg):
        print(f'[[Client Table Updated]]')
        self.peers = []
        for peer in json.loads(msg.data):
            self.peers.append(ClientInstance.from_json(peer))
        self.show_peers()

    def direct_message(self, message):
        if self.nickname != message.recipient and self.is_peer(message.recipient):
            message.msg_hash = hash(message)
            peer = self.get_peer(message.recipient)
            if peer.online:
                self.logger.debug(f'SEND: {message} to {peer}')
                try:
                    if not message.event_id == Events.ACK:
                        self.track_ack(message)
                        self.client.sendto(
                            message.to_json().encode(), (peer.ip, peer.port))
                        if not self.check_ack_timeout(message, TIMEOUT_MESSAGE, 0):
                            self.send_offline(message)
                            print(f'[No ACK from {message.recipient}, message sent to server.]')
                        else:
                            print(f'[Message received by {message.recipient}.]')
                    else:
                        self.client.sendto(
                            message.to_json().encode(), (peer.ip, peer.port))
                except Exception as e:
                    self.logger.info(
                        f'FAILED: send to: {peer} -> {e} -- DISABLING {peer}')
                    self.disable_client(peer)
            if not peer.online:
                self.send_offline(message)
        else:
            print(f'[[ Unknown peer: {message.recipient} ]]')
            self.logger.debug(f'!!UKNOWN: {message.recipient}!!')

    def send_offline(self, message):
        message.event_id = Events.OFFLINE_MESSAGE
        ts = datetime.datetime.now()
        message.data = str(ts) + ' ' + message.data
        self.server_message(message)

    def server_message(self, message):
        try:
            self.client.sendto(message.to_json().encode(
                'utf-8'), (self.server_ip, self.server_port))
        except Exception as e:
            self.logger.info(f'FAILED: send to: SERVER -> {e}')

    def client_receiver(self, sock):
        self.logger.debug(f'CLIENT RECEIVER: {sock}')
        while True:
            try:
                data, _ = sock.recvfrom(MSG_SIZE)
                self.handle_message(data)
            except Exception as e:
                self.logger.info(f'CLIENT: received error -> {e}')

    def client_thread(self):
        self.client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.client.bind((self.client_ip, self.client_port))
        print('Client IP->'+str(self.client_ip) +
              ' Port->'+str(self.client_port))
        print('Server IP->'+str(self.server_ip) +
              ' Port->'+str(self.server_port))

        Thread(target=self.client_receiver, args=(self.client,)).start()

        self.register(self.nickname)

        # TODO break out into a message handler method
        while True:
            message = {}
            data = input(">> ")

            if data.startswith('send_all '):
                parts = data.split()
                message = Message(event_id=Events.MESSAGE, nickname=self.nickname,
                                  data=' '.join(parts[1:]))
                self.server_message(message)

            if data.startswith('send '):
                parts = data.split()
                message = Message(event_id=Events.DIRECT_MESSAGE, nickname=self.nickname,
                                  recipient=parts[1], data=' '.join(parts[2:]))
                self.direct_message(message)
                continue

            elif data == '':
                continue

            elif data == 'peers':
                self.show_peers()
                continue

            elif data.startswith('dereg'):
                parts = data.split()
                if len(parts) != 1:
                    self.deregister(parts[1])
                else:
                    print('USAGE: dereg {nickname}')

            elif data.startswith('reg'):
                parts = data.split()
                if len(parts) != 1:
                    self.register(parts[1])
                else:
                    print('USAGE: reg {nickname}')

            elif data == 'quit':
                self.deregister(self.nickname)
                break

        self.client.close()
        os._exit(1)

    def handle_message(self, data):
        msg = Message.from_json(data)
        if msg.event_id in [Events.DIRECT_MESSAGE, 
                            Events.REGISTER_CONFIRM, Events.ERROR]:
            print(f'<<{msg.nickname}>> {msg.data}')
            self.logger.debug(f'SEND_ACK: {msg.nickname} with {msg.msg_hash}')
            self.send_ack(msg)

        if msg.event_id == Events.MESSAGE:
            print(f'[[{msg.nickname}]] {msg.data}')

        if msg.event_id == Events.ACK:
            self.handle_ack(msg)

        if msg.event_id == Events.CLIENT_UPDATE:
            self.update_clients(msg)

        if msg.event_id == Events.PING:
            ack_msg = Message(event_id=Events.ACK, nickname=self.nickname,
                          data=msg.msg_hash, recipient=msg.nickname)
            self.server_message(ack_msg)

def get_args():
    parser = argparse.ArgumentParser("Chat Application")
    parser.add_argument('mode', choices=['client', 'server'],
                        help='assign role - client or server')
    parser.add_argument('-n', '--name', help='name of the client', type=str)
    parser.add_argument(
        '-i', '--ip', help='specify ip for the server', type=str)
    parser.add_argument(
        '-p', '--port', help='specify port of the server', type=int)
    parser.add_argument('-c', '--client_port',
                        help='client port to bind to', type=int)
    return parser.parse_args()


def setup_logger():
    logger = logging.getLogger("ChatApp")
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        '%(asctime)s|%(name)s|%(levelname)s|%(message)s')

    info_handler = logging.StreamHandler()
    info_handler.setLevel(logging.INFO)
    info_handler.setFormatter(formatter)

    debug_handler = logging.FileHandler(filename='ChatApp.log', mode='a')
    debug_handler.setLevel(logging.DEBUG)
    debug_handler.setFormatter(formatter)

    logger.addHandler(debug_handler)
    logger.addHandler(info_handler)

    return logger


def main():
    logger = setup_logger()
    args = get_args()
    if args.mode == 'server':
        if args.port != None:
            Server(args.port, logger)
        else:
            print('-p/--port is required for server startup')
    elif args.mode == 'client':
        if args.name != None and args.client_port != None and args.ip != None:
            Client(args.name, args.client_port, args.ip, args.port, logger)
        else:
            print('-p/port, -i/ip, and -n/name are all required for client startup')


if __name__ == '__main__':
    main()
