import asyncio
import collections
from functools import (
    partial,
)
import hashlib
import re
import secrets
import struct

# How much we read at once. Messages _can_ be larger than this
MAX_READ = 16384

# Startup message (also called startup packets) don't have a type specified
START_MESSAGE_TYPE_LENGTH = 0
LATER_MESSAGE_TYPE_LENGTH = 1

# The length of messages itself takes 4 bytes
PAYLOAD_LENGTH_LENGTH = 4
PAYLOAD_LENGTH_FORMAT = "!L"

# This works when N is a response to aSSL request, but will probably go wrong
# if the server actually sends a Notice response, since that will be followed
# by data
NO_DATA_TYPE = b"N"
SSL_REQUEST_PAYLOAD = B"\x04\xd2\x16/"

Message = collections.namedtuple("Message", (
    "type", "payload_length", "payload"))
Processor = collections.namedtuple("Processor", (
    "c2s_from_outside", "c2s_from_inside", "s2c_from_outside", "s2c_from_inside"))

def postgres_message_parser(num_startup_messages):
    data_buffer = bytearray()
    messages_popped = 0

    def push_data(incoming_data):
        data_buffer.extend(incoming_data)

    def attempt_pop_message(type_length):
        """ Returns the next, possibly partly-received, message in data_buffer

        If the message is complete, then it's removed from the data buffer, and
        the return tuple's first component is True.
        """
        type_slice = slice(0, type_length)
        type_bytes = data_buffer[type_slice]
        has_type_bytes = len(type_bytes) == type_length

        # The documentation is a bit wrong: the 'N' type for no data, is _not_ followed
        # by a length
        payload_length_length = \
            0 if has_type_bytes and type_bytes == NO_DATA_TYPE else \
            PAYLOAD_LENGTH_LENGTH

        payload_length_slice = slice(type_length, type_length + payload_length_length)
        payload_length_bytes = data_buffer[payload_length_slice]
        has_payload_length_bytes = (
            has_type_bytes and len(payload_length_bytes) == payload_length_length
        )

        # The protocol specifies that the message length specified _includes_ MESSAGE_LENGTH_LENGTH,
        # so we subtract to get the actual length of the message.
        should_unpack = has_payload_length_bytes and payload_length_length
        payload_length = \
            unpack_length(payload_length_bytes) if should_unpack else \
            0

        payload_slice = slice(
            type_length + payload_length_length,
            type_length + payload_length_length + payload_length,
        )
        payload_bytes = data_buffer[payload_slice]
        has_payload_bytes = has_payload_length_bytes and len(payload_bytes) == payload_length
        message_length = type_length + payload_length_length + payload_length

        to_remove = \
            slice(0, message_length) if has_payload_bytes else \
            slice(0, 0)

        data_buffer[to_remove] = bytearray()

        return (
            has_payload_bytes,
            Message(bytes(type_bytes), bytes(payload_length_bytes), bytes(payload_bytes)),
        )

    def extract_messages(data):
        """ Yields a generator of Messages, each Message being the raw bytes of
        components of Postgres messages passed in data, or combined with that of
        previous calls where the data passed ended with an incomplete message

        The components of the triple:

          type of the message,
          the length of the payload,
          the payload itself,

        Each component is optional, and will be the empty byte if its not present
        Each Message is so constructed so that full original bytes can be retrieved
        by just concatanating them together, to make proxying easier
        """
        push_data(data)

        nonlocal messages_popped

        messages = []
        while True:
            pop_startup_message = messages_popped < num_startup_messages

            type_length = \
                START_MESSAGE_TYPE_LENGTH if pop_startup_message else \
                LATER_MESSAGE_TYPE_LENGTH
            has_popped, message = attempt_pop_message(type_length)

            if not has_popped:
                break

            messages_popped += 1
            messages.append(message)

        return messages

    return extract_messages


def postgres_parser_processor(to_c2s_outer, to_c2s_inner, to_s2c_outer, to_s2c_inner):
    c2s_parser = postgres_message_parser(num_startup_messages=2)
    s2c_parser = postgres_message_parser(num_startup_messages=0)

    def c2s_from_outside(data):
        messages = c2s_parser(data)
        to_c2s_inner(messages)

    def c2s_from_inside(messages):
        data = b"".join(flatten(messages))
        to_c2s_outer(data)

    def s2c_from_outside(data):
        messages = s2c_parser(data)
        to_s2c_inner(messages)

    def s2c_from_inside(messages):
        to_s2c_outer(b"".join(flatten(messages)))

    return Processor(c2s_from_outside, c2s_from_inside, s2c_from_outside, s2c_from_inside)


def postgres_log_processor(to_c2s_outer, to_c2s_inner, to_s2c_outer, to_s2c_inner):

    def log_all_messages(logging_title, messages):
        for message in messages:
            print(f"[{logging_title}] " + str(message))

    def c2s_from_outside(messages):
        log_all_messages('client->proxy', messages)
        to_c2s_inner(messages)

    def c2s_from_inside(messages):
        log_all_messages('proxy->server', messages)
        to_c2s_outer(messages)

    def s2c_from_outside(messages):
        log_all_messages('server->proxy', messages)
        to_s2c_inner(messages)

    def s2c_from_inside(messages):
        log_all_messages('proxy->client', messages)
        to_s2c_outer(messages)

    return Processor(c2s_from_outside, c2s_from_inside, s2c_from_outside, s2c_from_inside)


def postgres_auth_processor(to_c2s_outer, to_c2s_inner, to_s2c_outer, to_s2c_inner):
    # Experimental replacement of the username & password
    correct_client_password = b"proxy_mysecret"
    correct_server_password = b"mysecret"

    correct_client_username = b"proxy_postgres"

    # This could be returned back to the client, so it should _not_ be treated as secret
    correct_server_username = b"postgres"

    server_salt = None
    client_salt = None

    def to_server_startup(message):
        # The startup message seems to have an extra null character at the beginning,
        # which the documentation doesn't suggest

        pairs_list = re.compile(b"\x00([^\x00]+)\x00([^\x00]*)").findall(message.payload)
        pairs = dict(pairs_list)
        incorrect_user = md5(secrets.token_bytes(32))
        client_username = pairs[b'user']
        server_username = \
            correct_server_username if client_username == correct_client_username else \
            incorrect_user

        pairs_to_send = {**pairs, b'user': server_username}
        new_payload = b"\x00" + b"".join(flatten(
            (key, b"\x00", pairs_to_send[key], b"\x00")
            for key, _ in pairs_list
        )) + b"\x00"
        new_payload_length_bytes = pack_length(len(new_payload))

        return message._replace(payload_length=new_payload_length_bytes, payload=new_payload)

    def to_server_md5_response(message):
        client_md5 = message.payload[3:-1]
        correct_client_md5 = md5_salted(
            correct_client_password, correct_client_username, client_salt,
        )
        correct_server_md5 = md5_salted(
            correct_server_password, correct_server_username, server_salt,
        )
        md5_incorrect = md5(secrets.token_bytes(32))
        server_md5 = \
            correct_server_md5 if client_md5 == correct_client_md5 else \
            md5_incorrect
        return message._replace(payload=b"md5" + server_md5 + b"\x00")

    def c2s_from_outside(messages):
        for message in messages:
            is_startup = message.type == b"" and message.payload != SSL_REQUEST_PAYLOAD
            is_md5_response = message.type == b"p" and message.payload[0:3] == b"md5"
            message_to_yield = \
                to_server_startup(message) if is_startup else \
                to_server_md5_response(message) if is_md5_response else \
                message
            to_c2s_inner([message_to_yield])

    def c2s_from_inside(messages):
        to_c2s_outer(messages)

    def to_client_md5_request(message):
        return message._replace(payload=message.payload[0:4] + client_salt)

    def s2c_from_outside(messages):
        nonlocal server_salt
        nonlocal client_salt

        for message in messages:
            is_md5_request = message.type == b"R" and message.payload[0:4] == b"\x00\x00\x00\x05"
            server_salt, client_salt = \
                (message.payload[4:8], secrets.token_bytes(4)) if is_md5_request else \
                (server_salt, client_salt)
            message_to_yield = \
                to_client_md5_request(message) if is_md5_request else \
                message
            to_s2c_inner([message_to_yield])

    def s2c_from_inside(messages):
        to_s2c_outer(messages)

    return Processor(c2s_from_outside, c2s_from_inside, s2c_from_outside, s2c_from_inside)


def echo_processor(to_c2s_outer, _, to_s2c_outer, __):
    ''' Processor to not have to special case the innermost processor '''

    def c2s_from_outside(data):
        to_c2s_outer(data)

    def c2s_from_inside(_):
        pass

    def s2c_from_outside(data):
        to_s2c_outer(data)

    def s2c_from_inside(_):
        pass

    return Processor(c2s_from_outside, c2s_from_inside, s2c_from_outside, s2c_from_inside)


async def handle_client(client_reader, client_writer):
    try:
        server_reader, server_writer = await asyncio.open_connection("127.0.0.1", 5432)

        # Processors are akin to middlewares in a typical HTTP server. They are added,
        # "outermost" first, and can process the response of "inner" processors
        #
        # However, they are more complex since they...
        #
        # - Can send...
        #   - data to an inner processor destined for the client,
        #     typically in response to data from an outer processor from the server
        #   - data to an inner processor destined for the server,
        #     typically in response to data from an outer processor from the client
        #   - data to an outer processor destined for the client,
        #     typically in response to data from an inner processor from the server
        #   - data to an outer processor destined for the server,
        #     typically in response to data from an inner processor from the client
        #
        # - Can send multiple messages, not just the one response to a request

        def edge_to_c2s_outer(data):
            server_writer.write(data)

        def edge_to_s2c_outer(data):
            client_writer.write(data)

        def to_c2s_inner(i, data):
            return processors[i + 1].c2s_from_outside(data)

        def to_c2s_outer(i, data):
            return processors[i - 1].c2s_from_inside(data)

        def to_s2c_inner(i, data):
            return processors[i + 1].s2c_from_outside(data)

        def to_s2c_outer(i, data):
            return processors[i - 1].s2c_from_inside(data)

        outermost_processor = Processor(
            c2s_from_outside=partial(to_c2s_inner, 0),
            c2s_from_inside=edge_to_c2s_outer,
            s2c_from_outside=partial(to_s2c_inner, 0),
            s2c_from_inside=edge_to_s2c_outer,
        )

        processors = [
            outermost_processor,
        ] + [
            processor_constructor(
                partial(to_c2s_outer, i + 1),
                partial(to_c2s_inner, i + 1),
                partial(to_s2c_outer, i + 1),
                partial(to_s2c_inner, i + 1),
            )
            for i, processor_constructor in enumerate([
                postgres_parser_processor,
                postgres_log_processor,
                postgres_auth_processor,
                echo_processor,
            ])
        ]

        async def on_read(reader, on_data):
            while not reader.at_eof():
                data = await reader.read(MAX_READ)
                on_data(data)

        await asyncio.gather(
            on_read(client_reader, processors[0].c2s_from_outside),
            on_read(server_reader, processors[0].s2c_from_outside),
        )
    finally:
        client_writer.close()
        server_writer.close()


def unpack_length(length_bytes):
    return struct.unpack(PAYLOAD_LENGTH_FORMAT, length_bytes)[0] - PAYLOAD_LENGTH_LENGTH


def pack_length(length):
    return struct.pack(PAYLOAD_LENGTH_FORMAT, length + PAYLOAD_LENGTH_LENGTH)


def md5(data):
    return hashlib.md5(data).hexdigest().encode("utf-8")


def md5_salted(password, username, salt):
    return md5(md5(password + username) + salt)


def flatten(list_to_flatten):
    return (item for sublist in list_to_flatten for item in sublist)


async def async_main():
    await asyncio.start_server(handle_client, "0.0.0.0", 7777)


def main():
    loop = asyncio.get_event_loop()
    loop.run_until_complete(async_main())
    loop.run_forever()


if __name__ == "__main__":
    main()
