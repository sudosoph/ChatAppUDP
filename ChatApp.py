import socket
import os
from enum import Enum
from dataclasses import dataclass
from dataclasses_json import dataclass_json
import argparse
from threading import Thread
from queue import Queue
import json
import logging

# TODO Implement server-side client health checks
# TODO Implement ACK for message receipt, update receipt, etc.
# TODO Implement client message send ACK check -> server disable client
# TODO Implement offline messages
# TODO implement message hash to use for ACK so that we only ACK a specific message
# TODO client de-reg retry 5 times 500msec each
# TODO error if offline message sent when recipient is actually online
# TODO add timestamp to offline message .data property
# TODO add offline data structure to ClientInstance dataclass
# TODO clean up message formatting to match assignment examples

MSG_SIZE = 4096

class Events(Enum):
    REGISTER = 1
    DEREGISTER = 2
    REGISTER_CONFIRM = 3
    CLIENT_UPDATE = 4
    OFFLINE = 5
    ONLINE = 6
    MESSAGE = 10
    DIRECT_MESSAGE = 11
    OFFLINE_MESSAGE = 12
    ACK = 99

@dataclass_json
@dataclass
class Message:
    event_id: Events
    nickname: str
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

    def __str__(self):
        return f'{self.nickname} @ {self.ip}:{self.port}'

    def __eq__(self, other) -> bool:
        if self.nickname == other.nickname:
            return True
        return False

#Server Section
class Server:
       
    def __init__(self, port, logger):
        self.ip = socket.gethostbyname(socket.gethostname())
        self.port = port
        self.nickname = 'SERVER'
        self.server_persona = ClientInstance(
            nickname=self.nickname, ip=self.ip, port=self.port, online=True)
        self.clients = []
        self.inputBuffer = Queue()
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

    def enable_client(self,client):
        client.online = True
        self.logger.debug(f'ENABLED: {client}')
        self.update_clients()

    def register_client(self, addr, nickname):
        if self.is_client(nickname):
            client = self.get_client(nickname)
            client.ip = addr[0]
            client.port = int(addr[1])
            client.online = True
            self.logger.info(
                f'CLIENT UPDATED: {nickname} to {addr[0]}:{addr[1]}')
        else:
            print(f'CLIENT REGISTERED: {nickname} at {addr[0]}:{addr[1]}')
            client = ClientInstance(ip=addr[0], port=int(
                addr[1]), nickname=nickname, online=True)
            self.clients.append(client)
            self.logger.info(
                f'CLIENT REGISTERED: {nickname} to {addr[0]}:{addr[1]}')

        self.direct_message(self.server_persona, Message(event_id=Events.REGISTER_CONFIRM, nickname=self.nickname, 
                            recipient=client.nickname, data='[[Welcome, you are registered.]]'))
        self.update_clients()

    def update_clients(self):
        msg = Message(event_id=Events.CLIENT_UPDATE, nickname=self.nickname, 
                      data=json.dumps([client.to_json() for client in self.clients]))
        self.broadcast(msg, self.server_persona)
        self.logger.debug('Update clients')

    def server_thread(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # TODO remove for production
        self.ip = '127.0.0.1'
        self.server.bind((self.ip, self.port))
        self.logger.info(f'SERVER: {self.ip}:{self.port}')


        Thread(target=self.receiver_thread, args=()).start()

        while True:
            while not self.inputBuffer.empty():
                data, addr = self.inputBuffer.get()
                self.logger.debug(f'RECEIVED: {addr}: {data.decode()}')
                self.handle_message(data, addr)
        self.server.close()
        # TODO server shutdown method

    def receiver_thread(self):
        self.logger.info(f'RECEIVER_THREAD: {self.ip}:{self.port}')
        while True:
            data, addr = self.server.recvfrom(MSG_SIZE)
            self.inputBuffer.put((data,addr))
                
    def handle_message(self, data, addr):
        msg = Message.from_json(data)
        self.logger.debug(f'HANDLE: {addr} -> {msg.data}')

        if msg.event_id == Events.REGISTER:
            self.logger.info(f'REGISTER REQUEST: {addr}->{msg.nickname}')
            self.register_client(addr, msg.nickname)
            ack_msg = Message(event_id = Events.ACK, nickname = "Server", data = hash(msg), recipient = msg.nickname)
            self.direct_message(self.server_persona, ack_msg)

        if msg.event_id == Events.DEREGISTER:
            client = self.get_client(msg.nickname)
            self.logger.info(f'DISABLE REQUEST: {client.nickname}')
            self.disable_client(client)

        if msg.event_id == Events.MESSAGE:
            client = self.get_client(msg.nickname)
            if client is None:
                self.logger.info('ERROR: unknown sender - dropping message!')
            else:
                self.logger.info(f'BROADCAST: {client} {msg.data}')
                self.broadcast(msg, client)

        if msg.event_id == Events.DIRECT_MESSAGE:
            client = self.get_client(msg.nickname)
            self.logger.info(f'DIRECT MESSAGE: {client} -> {msg.recipient}: {msg.data}')
            if self.is_client(msg.recipient):
                self.direct_message(client, msg)
            else:
                self.logger.info(f'ERROR: {msg.recipient} is not a valid client')
                error = Message(event_id=Events.DIRECT_MESSAGE, nickname='SERVER', recipient=client.nickname, data=f'Unknown nickname {msg.recipient}')
                self.direct_message(self.server_persona, error)


    def direct_message(self, sending_client, message):
        if self.is_client(message.recipient):
            target_client = self.get_client(message.recipient)
            self.logger.debug(f'SEND: {message} from {sending_client} to {target_client}')
            try:
                self.server.sendto(message.to_json().encode(), (target_client.ip, target_client.port))
            except Exception as e:
                self.logger.info(
                    f'FAILED: send to: {target_client} -> {e} -- DISABLING {target_client}')
                self.disable_client(target_client)
        else:
            self.logger.info(f'UKNOWN: {message.recipient}')

    def broadcast(self, message, sending_client):
        self.logger.info(f'BROADCAST: {message} from {sending_client}')
        for client in self.clients:
            if client != sending_client and client.online:
                try:
                    self.logger.info(f'SENDING: {client}')
                    self.server.sendto(message.to_json().encode(), (client.ip, client.port))
                except Exception as e:
                    self.logger.info(f'FAILED: send to: {client} -> {e} -- DISABLING {client}')
                    self.disable_client(client)
    
# Client Section 
class Client:

    def __init__(self, name, client_port, ip, port, logger):
        self.server_ip = ip
        self.server_port = port
        self.client_port = client_port
        self.client_ip = socket.gethostbyname(socket.gethostname())
        self.online = True
        self.nickname = name
        self.peers = []
        self.logger = logger
        self.ack_checker = {}

        self.client_start()

    def is_peer(self, nickname):
        for peer in self.peers:
            if peer.nickname == nickname:
                return True
        return False

    def get_peer(self, nickname):
        for peer in self.peers:
            if peer.nickname == nickname:
                return peer

    def client_start(self):
        self.client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.client.bind((self.client_ip, self.client_port))
        print('Client IP->'+str(self.client_ip)+' Port->'+str(self.client_port))
        print('Server IP->'+str(self.server_ip)+' Port->'+str(self.server_port))

        reg_msg = Message(event_id=Events.REGISTER, nickname=self.nickname)
        self.logger.debug(f'REGISTERING: {reg_msg}')
        self.client.sendto(reg_msg.to_json().encode('utf-8'), (self.server_ip, self.server_port))

        Thread(target=self.client_receiver, args=(self.client,)).start()

        # TODO break out into a message handler method
        while True:
            message = {}
            data = input(">> ")
            if data.startswith('send_all '):
                parts = data.split()
                message = Message(event_id=Events.MESSAGE, nickname=self.nickname,
                                  data=' '.join(parts[1:]))
                self.client.sendto(message.to_json().encode('utf-8'), (self.server_ip, self.server_port))
            if data.startswith('send '):
                parts = data.split()
                message = Message(event_id=Events.DIRECT_MESSAGE, nickname=self.nickname, recipient=parts[1], data=' '.join(parts[2:]))
                self.direct_message(message)
                continue

            elif data == '':
                continue

            elif data == 'peers':
                print(self.peers)
                for peer in self.peers:
                    print(peer)
                continue

            elif data.startswith('dereg '):
                parts = data.split()
                message = Message(event_id=Events.DEREGISTER,
                                  nickname=parts[1])
                send_hash = hash(message)
                self.ack_checker[send_hash] = False
                self.nickname = parts[1]
                self.client.sendto(message.to_json().encode(
                    'utf-8'), (self.server_ip, self.server_port))
                
                for _ in range(5):
                    if not self.ack_checker[send_hash]:
                        os.sleep(0.5)
                    else:
                        self.online = True
                        continue
                if self.online == False:
                    self.logger.info("Server not responding")
                    self.logger.info("Exiting")
                    print("Server not responding")
                    print("Exiting")
                    os.exit()

            elif data.startswith('reg '):
                parts = data.split()
                message = Message(event_id=Events.REGISTER, nickname=parts[1])
                self.client.sendto(message.to_json().encode('utf-8'), (self.server_ip, self.server_port))
                self.online = False
                
            else:
                message = Message(event_id=Events.MESSAGE, nickname=self.nickname, data=data)
        # self.client.sendto(message.to_json().encode(
        #         'utf-8'), (self.server_ip, self.server_port))
        self.client.close()
        os._exit(1)

    def client_receiver(self, sock):
        # TODO make similar to server / handle incoming messages via Queue and process in main thread
        self.logger.debug(f'CLIENT RECEIVER: {sock}')
        while True:
            try:
                data, _ = sock.recvfrom(MSG_SIZE)
                self.handle_message(data)
            except Exception as e:
                self.logger.info(f'CLIENT: received error -> {e}')

    def handle_message(self, data):
        msg = Message.from_json(data)
        if msg.event_id in [Events.MESSAGE, Events.DIRECT_MESSAGE, Events.REGISTER_CONFIRM]:
            print(f'<<{msg.nickname}>> {msg.data}')
        
        if msg.event_id == Events.ACK:
            msg_hash = hash(msg)
            if msg_hash in self.ack_checker:
                self.ack_checker[msg_hash] = True
            else:
                self.logger.info(f'Invalid ACK from {self.nickname} with hash {msg_hash}')

        if msg.event_id == Events.CLIENT_UPDATE:
            print(f'[[Client Table Updated]]')
            self.peers = []
            for i, peer in enumerate(json.loads(msg.data)):
                self.peers.append(ClientInstance.from_json(peer))
                print(f'{i}: {self.peers[i]}')

    def direct_message(self, message):
        if self.is_peer(message.recipient):
            peer = self.get_peer(message.recipient)
            self.logger.info(f'SEND: {message} to {peer}')
            try:
                self.client.sendto(message.to_json().encode(), (peer.ip, peer.port))
            except Exception as e:
                self.logger.info(
                    f'FAILED: send to: {peer} -> {e} -- DISABLING {peer}')
                self.disable_client(peer)
        else:
            print(f'[[ Unknown peer: {message.recipient} ]]')
            self.logger.info(f'!!UKNOWN: {message.recipient}!!')


def get_args():
    parser = argparse.ArgumentParser("Chat Application")
    parser.add_argument('mode', choices=['client', 'server'], 
        help='assign role - client or server')
    parser.add_argument('-n', '--name', help='name of the client', type=str)
    parser.add_argument('-i', '--ip', help='specify ip for the server', type=str)
    parser.add_argument('-p', '--port', help='specify port of the server', type=int)
    parser.add_argument('-c', '--client_port', help='client port to bind to', type=int)
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
