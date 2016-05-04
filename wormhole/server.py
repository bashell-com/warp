import asyncio
import functools
import logging
import re
from time import time
from wormhole.authentication import get_ident
from wormhole.authentication import verify
from wormhole.handler import process_http
from wormhole.handler import process_https
from wormhole.logging import get_logger


MAX_RETRY = 3
MAX_TASKS = 1000
REGEX_CONTENT_LENGTH = re.compile(
    r'\r\nContent-Length: ([0-9]+)\r\n',
    re.IGNORECASE
)


async def process_request(client_reader, ident, loop):
    logger = get_logger()
    request_line = ''
    headers = []
    header = ''
    payload = b''
    try:
        retry = 0
        while True:
            line = await client_reader.readline()
            if not line:
                if len(header) == 0 and retry < MAX_RETRY:
                    # handle the case when the client make connection
                    # but sending data is delayed for some reasons
                    retry += 1
                    await asyncio.sleep(0.1, loop=loop)
                    continue
                else:
                    break
            if line == b'\r\n':
                break
            if line != b'':
                header += line.decode()
        m = REGEX_CONTENT_LENGTH.search(header)
        if m:
            cl = int(m.group(1))
            while len(payload) < cl:
                payload += await client_reader.read(1024)
    except Exception as e:
        logger.debug('!!! Task reject (%s)' % e, extra=ident)

    if header:
        header_lines = header.split('\r\n')
        if len(header_lines) > 1:
            request_line = header_lines[0]
        if len(header_lines) > 2:
            headers = header_lines[1:-1]

    return request_line, headers, payload


async def process_wormhole(client_reader, client_writer, cloaking, auth, loop):
    logger = get_logger()
    ident = get_ident(client_reader, client_writer)

    request_line, headers, payload = await process_request(
        client_reader, ident, loop
    )
    if not request_line:
        logger.debug((
            '[{id}][{client}]: !!! Task reject (empty request)'
        ).format(**ident))
        return

    request_fields = request_line.split(' ')
    if len(request_fields) == 2:
        request_method, uri = request_fields
        http_version = 'HTTP/1.0'
    elif len(request_fields) == 3:
        request_method, uri, http_version = request_fields
    else:
        logger.debug((
            '[{id}][{client}]: !!! Task reject (invalid request)'
        ).format(**ident))
        return

    if auth:
        user_ident = await verify(client_reader, client_writer, headers, auth)
        if user_ident is None:
            logger.info((
                '[{id}][{client}]: %s 407 %s' % (request_method, uri)
            ).format(**ident))
            return
        ident = user_ident

    if request_method == 'CONNECT':  # https proxy
        return await process_https(
            client_reader, client_writer, request_method, uri,
            ident, loop
        )
    else:
        return await process_http(
            client_writer, request_method, uri, http_version,
            headers, payload, cloaking,
            ident, loop
        )


wormhole_semaphore = None
def get_wormhole_semaphore(max_wormholes=MAX_TASKS, loop=None):
    global wormhole_semaphore
    if wormhole_semaphore is None:
        wormhole_semaphore = asyncio.Semaphore(max_wormholes, loop=loop)
    return wormhole_semaphore


async def limit_process(client_reader, client_writer, cloaking, auth, loop):
    async with get_wormhole_semaphore(loop=loop):
        await process_wormhole(
            client_reader, client_writer, cloaking, auth, loop
        )


clients = dict()
def accept_client(client_reader, client_writer, cloaking, auth, loop):
    logger = get_logger()
    ident = get_ident(client_reader, client_writer)
    task = asyncio.ensure_future(
        limit_process(client_reader, client_writer, cloaking, auth, loop),
        loop=loop
    )
    global clients
    clients[task] = (client_reader, client_writer)
    started_time = time()

    def client_done(task):
        del clients[task]
        client_writer.close()
        logger.debug((
            '[{id}][{client}]: Connection closed (%.5f seconds)' % (
                time() - started_time
            )
        ).format(**ident))

    logger.debug((
        '[{id}][{client}]: Connection started'
    ).format(**ident))
    task.add_done_callback(client_done)


async def start_wormhole_server(host, port, cloaking, auth, verbose, loop):
    logger = get_logger()
    if verbose > 0:
        logger.setLevel(logging.DEBUG)
    try:
        accept = functools.partial(
            accept_client, cloaking=cloaking, auth=auth, loop=loop
        )
        server = await asyncio.start_server(accept, host, port, loop=loop)
    except OSError as ex:
        logger.critical(
            '[000000][%s]: !!! Failed to bind server at [%s:%d]: %s' % (
                host, host, port, ex.args[1]
            )
        )
        raise
    else:
        logger.info(
            '[000000][%s]: wormhole bound at %s:%d' % (host, host, port)
        )
        return server