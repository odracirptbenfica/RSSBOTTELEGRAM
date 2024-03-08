from __future__ import annotations

import json
from typing import Union, Optional
from typing_extensions import Final
from collections.abc import Awaitable
import requests

import asyncio
import time
import aiographfix as aiograph
from io import BytesIO
from bs4 import BeautifulSoup
from contextlib import suppress
from aiohttp import ClientTimeout, ClientError
from aiohttp_retry import RetryClient
from aiohttp_socks import ProxyConnector

from .. import env, log
from .utils import is_emoticon, emojify, resolve_relative_link, isAbsoluteHttpLink
from .medium import construct_weserv_url
from ..aio_helper import run_async

convert_table_to_png: Optional[Awaitable]
if env.TABLE_TO_IMAGE:
    from .table_drawer import convert_table_to_png
else:
    convert_table_to_png = None

logger = log.getLogger('RSStT.tgraph')

apis: Optional[APIs] = None


async def init():
    global apis
    if env.TELEGRAPH_TOKEN:
        apis = APIs(env.TELEGRAPH_TOKEN)
        await apis.init()
        if not apis.valid:
            logger.error('Set up Telegraph failed, fallback to non-Telegraph mode.')
            apis = None


async def close():
    global apis
    if apis:
        await apis.close()
        apis = None


class Telegraph(aiograph.Telegraph):
    def __init__(self, token=None):
        self.last_run = 0
        self._fc_lock = asyncio.Lock()  # lock: wait if exceed flood control
        self._request_lock = asyncio.Lock()  # lock: only one request can be sent at the same time
        super().__init__(token)

    async def replace_session(self):
        await self.session.close()
        proxy_connector = ProxyConnector(**env.TELEGRAPH_PROXY_DICT, loop=self.loop) \
            if env.TELEGRAPH_PROXY_DICT else None
        self.session = RetryClient(connector=proxy_connector, timeout=ClientTimeout(total=10),
                                   loop=self.loop, json_serialize=self._json_serialize)

    async def create_page(self, *args, **kwargs) -> aiograph.types.Page:
        async with self._fc_lock:  # if not blocked, continue; otherwise, wait
            pass

        async with self._request_lock:
            await asyncio.sleep(max(0.5 - (time.time() - self.last_run), 0))  # avoid exceeding flood control
            page = await super().create_page(*args, **kwargs)
            self.last_run = time.time()
            return page

    async def flood_wait(self, retry_after: int):
        if not self._fc_lock.locked():  # if not already blocking
            async with self._fc_lock:  # block any other sending tries
                retry_after += 1
                logger.info('Blocking any requests for this telegraph account due to flood control... '
                            f'({retry_after}s)')
                if retry_after >= 60:
                    # create a now account if retry_after sucks
                    await self.create_account(short_name='RSStT', author_name='Generated by RSStT',
                                              author_url='https://github.com/Rongronggg9/RSS-to-Telegram-Bot')
                    logger.warning(f'Wanna let me wait? No way! Created a new Telegraph account: {self.token}')
                else:
                    await asyncio.sleep(retry_after)
                logger.info('Unblocked.')


class APIs:
    def __init__(self, tokens: Union[str, list[str]]):
        if isinstance(tokens, str):
            tokens = [tokens]
        self.tokens = tokens
        self._accounts: list[Telegraph] = []
        self._curr_id = 0

    async def init(self):
        for token in self.tokens:
            token = token.strip()
            account = Telegraph(token)
            await account.replace_session()
            try:
                if len(token) != 60:  # must be an invalid token
                    logger.warning('Telegraph API token may be invalid, create one instead.')
                    await account.create_account(short_name='RSStT', author_name='Generated by RSStT',
                                                 author_url='https://github.com/Rongronggg9/RSS-to-Telegram-Bot')
                await account.get_account_info()
                self._accounts.append(account)
            except aiograph.exceptions.TelegraphError as e:
                logger.warning(f'Telegraph API token may be invalid, create one instead: {e}')
                try:
                    await account.create_account(short_name='RSStT', author_name='Generated by RSStT',
                                                 author_url='https://github.com/Rongronggg9/RSS-to-Telegram-Bot')
                    self._accounts.append(account)
                except Exception as e:
                    logger.warning(f'Cannot set up one of Telegraph accounts: {e}', exc_info=e)
            except Exception as e:
                logger.warning(f'Cannot set up one of Telegraph accounts: {e}', exc_info=e)

    async def close(self):
        for account in self._accounts:
            await account.close()

    @property
    def valid(self):
        return bool(self._accounts)

    @property
    def count(self):
        return len(self._accounts)

    def get_account(self) -> Telegraph:
        if not self._accounts:
            raise aiograph.exceptions.TelegraphError('Telegraph token no set!')

        curr_id = self._curr_id if 0 <= self._curr_id < len(self._accounts) else 0
        self._curr_id = curr_id + 1 if 0 <= curr_id + 1 < len(self._accounts) else 0
        return self._accounts[curr_id]


TELEGRAPH_ALLOWED_TAGS: Final = {
    'a', 'aside', 'b', 'blockquote', 'br', 'code', 'em', 'figcaption', 'figure',
    'h3', 'h4', 'hr', 'i', 'iframe', 'img', 'li', 'ol', 'p', 'pre', 's',
    'strong', 'u', 'ul', 'video'
}

TELEGRAPH_REPLACE_TAGS: Final = {
    'strong': 'b',
    'em': 'i',
    'strike': 's',
    'del': 's',
    'ins': 'u',
    'big': 'b',
    'h1': 'h3',
    'h2': 'h4',
    'h3': 'b',
    'h4': 'u',
    'h5': 'p',
    'h6': 'p',
    'details': 'blockquote',
}

TELEGRAPH_TAGS_INSERT_BR_AFTER: Final = {
    'div', 'section', 'tr'
}

TELEGRAPH_DEL_TAGS: Final = {
    'table', 'svg', 'script', 'noscript', 'style', 'head', 'source'
}

TELEGRAPH_DISALLOWED_TAGS_IN_LI: Final = {
    'p', 'section'
}

TELEGRAPH_TAGS_ALLOW_ATTR: Final = {
    'a': 'href',
    'img': 'src',
    'iframe': 'src',
    'video': 'src',
}


class TelegraphIfy:
    def __init__(self, html: str = None, title: str = None, link: str = None, feed_title: str = None,
                 author: str = None, feed_link: str = None):
        self.retries = 0

        if not apis:
            raise aiograph.exceptions.TelegraphError('Telegraph token no set!')

        self.html = emojify(html)
        self.title = title
        self.link = link
        self.feed_title = feed_title
        self.author = author
        self.feed_link = feed_link

        self.telegraph_author = None
        self.telegraph_author_url = None
        self.telegraph_title = None
        self.telegraph_html_content = None

        self.task = env.loop.create_task(self.generate_page())

    async def generate_page(self):
        soup = await run_async(BeautifulSoup, self.html, 'lxml', prefer_pool='thread')

        for tag in soup.find_all(recursive=True):
            with suppress(ValueError, AttributeError):
                # add linebreak after certain tags
                if tag.name in TELEGRAPH_TAGS_INSERT_BR_AFTER:
                    tag.insert_after(soup.new_tag('br'))

                # remove tags that are not allowed in <li>
                if tag.name == 'li':
                    disallowed_tags = tag.find_all(TELEGRAPH_DISALLOWED_TAGS_IN_LI, recursive=True)
                    for disallowed_tag in disallowed_tags:
                        disallowed_tag.replaceWithChildren()

                # deal with inline quotes
                if tag.name == 'q':
                    tag.insert_before('“')
                    tag.insert_after('”')
                    cite = tag.get('cite')
                    if cite:
                        tag.name = 'a'
                        tag['href'] = cite
                        del tag['cite']
                    else:
                        tag.replaceWithChildren()

                # deal with tags itself
                if tag.name in TELEGRAPH_DEL_TAGS:
                    if tag.name == 'table':
                        rows = tag.find_all('tr')
                        if not rows:
                            tag.decompose()
                            continue
                        for row in rows:
                            columns = row.find_all(('td', 'th'))
                            if len(columns) != 1:
                                if env.TABLE_TO_IMAGE:
                                    table_img = await convert_table_to_png(str(tag))
                                    if table_img:
                                        with BytesIO(table_img) as buffer:
                                            url_l = await apis.get_account().upload(buffer, full=False)
                                        url = url_l[0] if url_l else None
                                        if url:
                                            tag.replaceWith(soup.new_tag('img', src=url))
                                            continue
                                tag.decompose()
                                continue
                        tag.replaceWithChildren()
                    else:
                        tag.decompose()
                    continue
                elif tag.name in TELEGRAPH_REPLACE_TAGS:
                    old_name = tag.name
                    new_name = TELEGRAPH_REPLACE_TAGS[old_name]
                    tag.name = new_name
                    if old_name.startswith('h') and not new_name.startswith('h') and new_name != 'p':
                        # ensure take a whole line
                        tag.insert_before(soup.new_tag('br')) \
                            if (hasattr(tag.previous_sibling, 'name')
                                and tag.previous_sibling.name not in {'br', 'p'}
                                and not tag.previous_sibling.name.startswith('h')) \
                            else None
                        tag.insert_after(soup.new_tag('br'))
                elif tag.name not in TELEGRAPH_ALLOWED_TAGS:
                    tag.replaceWithChildren()  # remove disallowed tags
                    continue
                # verify tags
                if tag.name == 'a' and not tag.text:
                    tag.replaceWithChildren()  # remove invalid links
                    continue
                elif tag.name == 'img' and is_emoticon(tag):
                    alt = tag.get('alt')
                    tag.replaceWith(alt) if alt else tag.decompose()  # drop emoticon
                    continue
                # deal with attributes
                if tag.name not in TELEGRAPH_TAGS_ALLOW_ATTR:
                    tag.attrs = {}  # remove all attributes
                    continue
                else:
                    attr_name = TELEGRAPH_TAGS_ALLOW_ATTR[tag.name]
                    attr_content = tag.attrs.get(attr_name)
                    if not attr_content:
                        tag.replaceWithChildren()
                        continue
                    attr_content = resolve_relative_link(self.feed_link, attr_content)
                    if not isAbsoluteHttpLink(attr_content):
                        tag.replaceWithChildren()
                        continue
                    if not attr_content.startswith(env.IMG_RELAY_SERVER):
                        if tag.name == 'video':
                            attr_content = env.IMG_RELAY_SERVER + attr_content
                        if tag.name == 'img' and not attr_content.startswith(env.IMAGES_WESERV_NL):
                            if attr_content.split('.', 1)[1].split('/', 1)[0] == 'sinaimg.cn':
                                attr_content = env.IMG_RELAY_SERVER + attr_content
                            if env.TELEGRAPH_IMG_UPLOAD:
                                attr_content = telegraph_file_upload(attr_content)
                            else:
                                attr_content = construct_weserv_url(attr_content)
                            logger.warning(f'Processed img: {attr_content}')
                    tag.attrs = {attr_name: attr_content}

        if self.feed_title:
            self.telegraph_author = f"{self.feed_title}"
            if self.author and self.author not in self.feed_title:
                self.telegraph_author += f' ({self.author})'
            self.telegraph_author_url = self.link or ''
        else:
            self.telegraph_author = 'Generated by RSStT'
            self.telegraph_author_url = 'https://github.com/Rongronggg9/RSS-to-Telegram-Bot'

        self.telegraph_title = self.title or 'Generated by RSStT'
        self.telegraph_html_content = (soup.decode() +
                                       '<p>Generated by '
                                       '<a href="https://github.com/Rongronggg9/RSS-to-Telegram-Bot">RSStT</a>. '
                                       'The copyright belongs to the original author.</p>'
                                       # "If images cannot be loaded properly due to anti-hotlinking, "
                                       # "please consider install "
                                       # "<a href='https://greasyfork.org/scripts/432923'>this userscript</a>."
                                       + (f'<p><a href="{self.link}">Source</a></p>' if self.link else ''))

    async def telegraph_ify(self):
        await self.task  # wait for the page to be fully created

        if self.retries >= 3:
            raise OverflowError

        if self.retries >= 1:
            logger.debug('Retrying using another telegraph account...' if apis.count > 1 else 'Retrying...')

        telegraph_account = apis.get_account()
        try:
            telegraph_page = await telegraph_account.create_page(
                title=f'{self.telegraph_title[:60]}…' if len(self.telegraph_title) > 61 else self.telegraph_title,
                content=self.telegraph_html_content,
                author_name=self.telegraph_author[:128],
                author_url=self.telegraph_author_url[:512]
            )
            return telegraph_page.url
        except aiograph.exceptions.TelegraphError as e:
            e_msg = str(e)
            if e_msg.startswith('FLOOD_WAIT_'):  # exceed flood control
                retry_after = int(e_msg.split('_')[-1])
                logger.debug(f'Flood control exceeded. Wait {retry_after}.0 seconds')
                self.retries += 1
                rets = await asyncio.gather(telegraph_account.flood_wait(retry_after), self.telegraph_ify())

                return rets[0]
            raise e
        except (TimeoutError, asyncio.TimeoutError) as e:
            raise e  # aiohttp_retry will retry automatically, so it means too many retries if caught
        except (ClientError, ConnectionError) as e:
            if self.retries < 3:
                logger.debug(
                    f'Network error ({type(e).__name__}) occurred when creating telegraph page, will retry')
                return await self.telegraph_ify()
            raise e


def telegraph_file_upload(file_url):
    '''
    Sends a file to telegra.ph storage and returns its url
    Works ONLY with 'gif', 'jpeg', 'jpg', 'png', 'mp4'

    Parameters
    ---------------
    path_to_file -> str, path to a local file

    Return
    ---------------
    telegraph_url -> str, url of the file uploaded

    >>>telegraph_file_upload('test_image.jpg')
    https://telegra.ph/file/16016bafcf4eca0ce3e2b.jpg
    >>>telegraph_file_upload('untitled.txt')
    error, txt-file can not be processed
    '''
    response = requests.get(file_url)
    if response.status_code == 200:
        # 提取图片内容
        image_data = response.content
        url = 'https://telegra.ph/upload'
        upload_response = requests.post(url, files={'file': ('file', image_data)})
        telegraph_url = json.loads(upload_response.content)
        telegraph_url = telegraph_url[0]['src']
        telegraph_url = f'https://telegra.ph{telegraph_url}'
        return telegraph_url
    return construct_weserv_url(file_url)
