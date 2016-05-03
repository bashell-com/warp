import asyncio
import re
from socket import TCP_NODELAY
from wormhole.cloaking import cloak
from wormhole.logging import get_logger


REGEX_HOST = re.compile(r'(.+?):([0-9]{1,5})')
logger = get_logger()


async def process_https(client_reader, client_writer, request_method, uri,
                        ident, loop):
    m = REGEX_HOST.search(uri)
    host = m.group(1)
    port = int(m.group(2))
    if port == 443:
        url = 'https://%s/' % host
    else:
        url = 'https://%s:%s/' % (host, port)
    try:
        logger.info((
            '[{id}][{client}]: %s 200 %s' % (request_method, url)
        ).format(**ident))
        req_reader, req_writer = await asyncio.open_connection(
            host, port, ssl=False, loop=loop
        )
        client_writer.write(b'HTTP/1.1 200 Connection established\r\n')
        client_writer.write(b'\r\n')

        async def relay_stream(reader, writer):
            try:
                while True:
                    line = await reader.read(1024)
                    if len(line) == 0:
                        break
                    writer.write(line)
            except:
                logger.info((
                    '[{id}][{client}]: %s 502 %s' % (request_method, url)
                ).format(**ident))

        tasks = [
            asyncio.ensure_future(
                relay_stream(client_reader, req_writer), loop=loop),
            asyncio.ensure_future(
                relay_stream(req_reader, client_writer), loop=loop),
        ]
        await asyncio.wait(tasks, loop=loop)
    except:
        logger.info((
            '[{id}][{client}]: %s 502 %s' % (request_method, url)
        ).format(**ident))


async def process_http(client_writer, request_method, uri, http_version,
                       headers, payload, cloaking, ident, loop):
    phost = False
    sreq = []
    sreqHeaderEndIndex = 0
    has_connection_header = False

    for header in headers:
        headerNameAndValue = header.split(': ', 1)

        if len(headerNameAndValue) == 2:
            headerName, headerValue = headerNameAndValue
        else:
            headerName, headerValue = headerNameAndValue[0], None

        if headerName.lower() == "host":
            phost = headerValue
        elif headerName.lower() == "connection":
            has_connection_header = True
            if headerValue.lower() in ('keep-alive', 'persist'):
                # current version of this program does not support
                # the HTTP keep-alive feature
                sreq.append("Connection: close")
            else:
                sreq.append(header)
        elif headerName.lower() != 'proxy-connection':
            sreq.append(header)
            if len(header) == 0 and sreqHeaderEndIndex == 0:
                sreqHeaderEndIndex = len(sreq) - 1

    if sreqHeaderEndIndex == 0:
        sreqHeaderEndIndex = len(sreq)

    if not has_connection_header:
        sreq.insert(sreqHeaderEndIndex, "Connection: close")

    if not phost:
        phost = '127.0.0.1'

    path = uri[len(phost) + 7:]  # 7 is len('http://')
    new_head = ' '.join([request_method, path, http_version])

    m = REGEX_HOST.search(phost)
    if m:
        host = m.group(1)
        port = int(m.group(2))
    else:
        host = phost
        port = 80

    response_status = None
    response_code = None
    try:
        req_reader, req_writer = await asyncio.open_connection(
            host, port, flags=TCP_NODELAY, loop=loop
        )
        req_writer.write(('%s\r\n' % new_head).encode())
        await req_writer.drain()
        await asyncio.sleep(0.01, loop=loop)

        if cloaking:
            await cloak(req_writer, phost, loop)
        else:
            req_writer.write(b'Host: ' + phost.encode())
        req_writer.write(b'\r\n')

        [req_writer.write((header + '\r\n').encode()) for header in sreq]
        req_writer.write(b'\r\n')

        if payload != b'':
            req_writer.write(payload)
            req_writer.write(b'\r\n')
        await req_writer.drain()

        try:
            while True:
                buf = await req_reader.read(1024)
                if response_status is None:
                    response_status = buf[:buf.find(b'\r\n')]
                if len(buf) == 0:
                    break
                client_writer.write(buf)
        except:
            response_code = '502'
    except:
        response_code = '502'

    if response_code is None:
        response_code = response_status.decode('ascii').split(' ')[1]
    logger.info((
        '[{id}][{client}]: %s %s %s' % (request_method, response_code, uri)
    ).format(**ident))
