from __future__ import annotations
from collections.abc import Callable
from typing import Union, Optional, AnyStr, Any
from src.compat import nullcontext, ssl_create_default_context

import re
import asyncio
import functools
import aiodns
import aiohttp
import feedparser
import PIL.Image
import PIL.ImageFile
from PIL import UnidentifiedImageError
from bs4 import BeautifulSoup
from io import BytesIO, SEEK_END
from concurrent.futures import ThreadPoolExecutor
from aiohttp_socks import ProxyConnector
from aiohttp_retry import RetryClient, ExponentialRetry
from ssl import SSLError
from ipaddress import ip_network, ip_address
from urllib.parse import urlparse
from socket import AF_INET, AF_INET6
from multidict import CIMultiDictProxy
from attr import define
from functools import partial
from asyncstdlib.functools import lru_cache

from src import env, log, locks
from src.i18n import i18n

PROXY = env.R_PROXY.replace('socks5h', 'socks5').replace('sock4a', 'socks4') if env.R_PROXY else None
PRIVATE_NETWORKS = tuple(ip_network(ip_block) for ip_block in
                         ('127.0.0.0/8', '::1/128',  # loopback is not a private network, list in here for convenience
                          '169.254.0.0/16', 'fe80::/10',  # link-local address
                          '10.0.0.0/8',  # class A private network
                          '172.16.0.0/12',  # class B private networks
                          '192.168.0.0/16',  # class C private networks
                          'fc00::/7',  # ULA
                          ))

HEADER_TEMPLATE = {
    'User-Agent': env.USER_AGENT,
    'Accept': '*/*',
    'Accept-Encoding': 'gzip, deflate, br',
}
FEED_ACCEPT = 'application/rss+xml, application/rdf+xml, application/atom+xml, ' \
              'application/xml;q=0.9, text/xml;q=0.8, text/*;q=0.7, application/*;q=0.6'

EXCEPTIONS_SHOULD_RETRY = (asyncio.TimeoutError,
                           # aiohttp.ClientPayloadError,
                           # aiohttp.ClientResponseError,
                           # aiohttp.ClientConnectionError,
                           aiohttp.ServerConnectionError,
                           TimeoutError)

RETRY_OPTION = ExponentialRetry(attempts=2, start_timeout=1, exceptions=set(EXCEPTIONS_SHOULD_RETRY))

PIL.ImageFile.LOAD_TRUNCATED_IMAGES = True

logger = log.getLogger('RSStT.web')

_feedparser_thread_pool = ThreadPoolExecutor(1, 'feedparser_')
_resolver = aiodns.DNSResolver(timeout=3, loop=env.loop)

contentDispositionFilenameParser = partial(re.compile(r'(?<=filename=").+?(?=")').search, flags=re.I)


class WebError(Exception):
    def __init__(self, error_name: str, status: Union[int, str] = None, url: str = None,
                 base_error: Exception = None, hide_base_error: bool = False, log_level: int = log.DEBUG):
        super().__init__(error_name)
        self.error_name = error_name
        self.status = status
        self.url = url
        self.base_error = base_error
        self.hide_base_error = hide_base_error
        log_msg = f'Fetch failed ({error_name}'
        log_msg += (f', {type(base_error).__name__}'
                    if not hide_base_error and base_error and log_level < log.ERROR
                    else '')
        log_msg += f', {status}' if status else ''
        log_msg += ')'
        log_msg += f': {url}' if url else ''
        logger.log(log_level,
                   log_msg,
                   exc_info=base_error if not hide_base_error and base_error and log_level >= log.ERROR else None)

    def i18n_message(self, lang: str = None) -> str:
        error_key = self.error_name.lower().replace(' ', '_')
        msg = f'ERROR: {i18n[lang][error_key]}'
        if not self.hide_base_error and self.base_error:
            msg += f' ({type(self.base_error).__name__})'
        if self.status:
            msg += f' ({self.status})'
        return msg

    def __str__(self) -> str:
        return self.i18n_message()


@define
class WebResponse:
    url: str  # redirected url
    content: Any
    headers: CIMultiDictProxy[str]
    status: int
    reason: Optional[str]


@define
class WebFeed:
    url: str  # redirected url
    headers: Optional[CIMultiDictProxy[str]] = None
    status: int = -1
    reason: Optional[str] = None
    rss_d: Optional[feedparser.FeedParserDict] = None
    error: Optional[WebError] = None


def proxy_filter(url: str, parse: bool = True) -> bool:
    if not (env.PROXY_BYPASS_PRIVATE or env.PROXY_BYPASS_DOMAINS):
        return True

    hostname = urlparse(url).hostname if parse else url
    if env.PROXY_BYPASS_PRIVATE:
        try:
            ip_a = ip_address(hostname)
            is_private = any(ip_a in network for network in PRIVATE_NETWORKS)
            if is_private:
                return False
        except ValueError:
            pass  # not an IP, continue
    if env.PROXY_BYPASS_DOMAINS:
        is_bypassed = any(hostname.endswith(domain) and (hostname == domain or hostname[-len(domain) - 1] == '.')
                          for domain in env.PROXY_BYPASS_DOMAINS)
        if is_bypassed:
            return False
    return True


async def __norm_callback(response: aiohttp.ClientResponse, decode: bool = False, max_size: Optional[int] = None,
                          intended_content_type: Optional[str] = None) -> Optional[AnyStr]:
    content_type = response.headers.get('Content-Type')
    if not intended_content_type or not content_type or content_type.startswith(intended_content_type):
        if decode:
            return await response.text()
        elif max_size is None:
            return await response.read()
        elif max_size > 0:
            max_size = min(int(response.headers.get('Content-Length', str(max_size))),
                           max_size)
            return await response.content.read(max_size)
    return None


async def get(url: str, timeout: Optional[float] = None, semaphore: Union[bool, asyncio.Semaphore] = None,
              headers: Optional[dict] = None, decode: bool = False,
              max_size: Optional[int] = None, intended_content_type: Optional[str] = None) -> WebResponse:
    """
    :param url: URL to fetch
    :param timeout: timeout in seconds
    :param semaphore: semaphore to use for limiting concurrent connections
    :param headers: headers to use
    :param decode: whether to decode the response body (cannot mix with max_size)
    :param max_size: maximum size of the response body (in bytes), None=unlimited, 0=ignore response body
    :param intended_content_type: if specified, only return response if the content-type matches
    :return: {url, content, headers, status}
    """
    if not timeout:
        timeout = 12
    wait_for_timeout = (timeout * 2 + 5) * (2 if env.IPV6_PRIOR else 1)
    return await asyncio.wait_for(
        _get(
            url=url, timeout=timeout, semaphore=semaphore, headers=headers,
            resp_callback=partial(__norm_callback, decode=decode, max_size=max_size,
                                  intended_content_type=intended_content_type)
        ),
        wait_for_timeout)


async def _get(url: str, resp_callback: Callable, timeout: Optional[float] = None,
               semaphore: Union[bool, asyncio.Semaphore] = None, headers: Optional[dict] = None) -> WebResponse:
    host = urlparse(url).hostname
    semaphore_to_use = locks.hostname_semaphore(host, parse=False) if semaphore in (None, True) \
        else (semaphore or nullcontext())
    v6_address = None
    try:
        v6_address = await asyncio.wait_for(_resolver.query(host, 'AAAA'), timeout=1) if env.IPV6_PRIOR else None
    except (aiodns.error.DNSError, asyncio.TimeoutError):
        pass
    except Exception as e:
        logger.debug(f'Error occurred when querying {url} AAAA:', exc_info=e)
    socket_family = AF_INET6 if v6_address else 0

    _headers = HEADER_TEMPLATE.copy()
    if headers:
        _headers.update(headers)

    tries = 0
    retry_in_v4_flag = False
    while True:
        tries += 1
        assert tries <= 2, 'Too many tries'

        if retry_in_v4_flag:
            socket_family = AF_INET
        ssl_context = ssl_create_default_context()
        proxy_connector = (
            ProxyConnector.from_url(PROXY, family=socket_family, ssl=ssl_context)
            if (PROXY and proxy_filter(host, parse=False))
            else aiohttp.TCPConnector(family=socket_family, ssl=ssl_context)
        )

        try:
            async with locks.overall_web_semaphore:
                async with semaphore_to_use:
                    async with RetryClient(retry_options=RETRY_OPTION, connector=proxy_connector,
                                           timeout=aiohttp.ClientTimeout(total=timeout), headers=_headers) as session:
                        async with session.get(url) as response:
                            status = response.status
                            content = None
                            if status == 200:
                                content = await resp_callback(response)
                            elif socket_family == AF_INET6 and tries == 1 \
                                    and status in (400,  # Bad Request (some feed providers return 400 for banned IPs)
                                                   403,  # Forbidden
                                                   429,  # Too Many Requests
                                                   451):  # Unavailable For Legal Reasons
                                retry_in_v4_flag = True
                                continue
                            return WebResponse(url=url,
                                               content=content,
                                               headers=response.headers,
                                               status=status,
                                               reason=response.reason)
        except EXCEPTIONS_SHOULD_RETRY as e:
            if socket_family != AF_INET6 or tries > 1:
                raise e
            err_msg = str(e).strip()
            logger.debug(f'Fetch failed ({type(e).__name__}' + (f': {err_msg}' if err_msg else '')
                         + f') using IPv6, retrying using IPv4: {url}')
            retry_in_v4_flag = True
            continue


async def feed_get(url: str, timeout: Optional[float] = None, web_semaphore: Union[bool, asyncio.Semaphore] = None,
                   headers: Optional[dict] = None, verbose: bool = True) -> WebFeed:
    ret = WebFeed(url=url)

    log_level = log.WARNING if verbose else log.DEBUG
    _headers = {}
    if headers:
        _headers.update(headers)
    if 'Accept' not in _headers:
        _headers['Accept'] = FEED_ACCEPT

    try:
        resp = await get(url, timeout, web_semaphore, headers=_headers)
        rss_content = resp.content
        ret.url = resp.url
        ret.headers = resp.headers
        ret.status = resp.status

        # some rss feed implement http caching improperly :(
        if resp.status == 200 and int(resp.headers.get('Content-Length', '1')) == 0:
            ret.status = 304
            # ret.msg = f'"Content-Length" is 0'
            return ret

        if resp.status == 304:
            # ret.msg = f'304 Not Modified'
            return ret  # 304 Not Modified, feed not updated

        if rss_content is None:
            status_caption = f'{resp.status}' + (f' {resp.reason}' if resp.reason else '')
            ret.error = WebError(error_name='status code error', status=status_caption, url=url, log_level=log_level)
            return ret

        if len(rss_content) <= 524288:
            rss_d = feedparser.parse(rss_content, sanitize_html=False)
        else:  # feed too large, run in another thread to avoid blocking the bot
            rss_d = await asyncio.get_event_loop().run_in_executor(_feedparser_thread_pool,
                                                                   functools.partial(feedparser.parse,
                                                                                     rss_content,
                                                                                     sanitize_html=False))

        if 'title' not in rss_d.feed:
            ret.error = WebError(error_name='feed invalid', url=url, log_level=log_level)
            return ret

        ret.rss_d = rss_d
    except aiohttp.InvalidURL:
        ret.error = WebError(error_name='URL invalid', url=url, log_level=log_level)
    except (asyncio.TimeoutError,
            aiohttp.ClientError,
            SSLError,
            OSError,
            ConnectionError,
            TimeoutError) as e:
        ret.error = WebError(error_name='network error', url=url, base_error=e, log_level=log_level)
    except Exception as e:
        ret.error = WebError(error_name='internal error', url=url, base_error=e, log_level=log.ERROR)

    return ret


async def __medium_info_callback(response: aiohttp.ClientResponse) -> tuple[int, int]:
    content_type = response.headers.get('Content-Type', '').lower()
    content_length = int(response.headers.get('Content-Length', '1024'))
    max_read_length = min(content_length, 5 * 1024)
    if not (
            # a non-webp image
            (content_type.startswith('image') and content_type.find('webp') == -1)
            or (
                    # a webp image
                    (content_type.find('webp') != -1 or content_type == 'application/octet-stream')
                    and content_length <= max_read_length  # PIL cannot handle a truncated webp image
            )
    ):
        return -1, -1
    is_jpeg = content_type.startswith('image/jpeg')
    already_read = 0
    iter_length = 128
    buffer = BytesIO()
    async for chunk in response.content.iter_chunked(iter_length):
        already_read += len(chunk)
        buffer.seek(0, SEEK_END)
        buffer.write(chunk)
        # noinspection PyBroadException
        try:
            image = PIL.Image.open(buffer)
            width, height = image.size
            return width, height
        except UnidentifiedImageError:
            return -1, -1  # not a format that PIL can handle
        except Exception:
            if is_jpeg:
                file_header = buffer.getvalue()
                pointer = -1
                for marker in (b'\xff\xc2', b'\xff\xc1', b'\xff\xc0'):
                    p = file_header.find(marker)
                    if p != -1:
                        pointer = p
                        break
                if pointer != -1 and pointer + 9 <= len(file_header):
                    width = int(file_header[pointer + 7:pointer + 9].hex(), 16)
                    height = int(file_header[pointer + 5:pointer + 7].hex(), 16)
                    return width, height

        if already_read >= max_read_length:
            return -1, -1
    return -1, -1


@lru_cache(maxsize=512)
async def get_medium_info(url: str) -> Optional[tuple[int, int, int, Optional[str]]]:
    if url.startswith('data:'):
        return None
    try:
        r = await _get(url, resp_callback=__medium_info_callback)
        if r.status != 200:
            raise ValueError('status code not 200')
    except Exception as e:
        logger.debug(f'Medium fetch failed: {url}', exc_info=e)
        return None

    width, height = -1, -1
    size = int(r.headers.get('Content-Length') or -1)
    content_type = r.headers.get('Content-Type')
    if isinstance(r.content, tuple):
        width, height = r.content

    return size, width, height, content_type


@lru_cache(maxsize=256)
async def get_page_title(url: str, allow_hostname=True, allow_path: bool = False, allow_filename: bool = True) \
        -> Optional[str]:
    r = None
    # noinspection PyBroadException
    try:
        r = await get(url=url, timeout=2, decode=True, intended_content_type='text/html')
        if r.status != 200 or not r.content:
            raise ValueError('not an HTML page')
        if len(r.content) <= 27:  # len of `<html><head><title></title>`
            raise ValueError('invalid HTML')
        title = BeautifulSoup(r.content, 'lxml').title.text
        return title.strip()
    except Exception:
        content_disposition = r.headers.get('Content-Disposition') if r else None
        filename_match = contentDispositionFilenameParser(content_disposition) if content_disposition else None
        if filename_match and allow_filename:
            return filename_match.group()
        url_parsed = urlparse(url)
        if allow_path:
            path = url_parsed.path
            return path.rsplit('/', 1)[-1] if path else None
        if allow_hostname:
            return url_parsed.hostname
