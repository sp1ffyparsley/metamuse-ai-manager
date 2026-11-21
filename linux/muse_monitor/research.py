"""Bounded public research through official APIs, HTML and RSS."""
import hashlib
import json
import re
import threading
import time
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

from .store import Conflict

HOSTS = {'api.github.com', 'docs.github.com', 'developer.mozilla.org', 'docs.blender.org',
         'developers.google.com', 'hermes-agent.nousresearch.com', 'help.openai.com',
         'www.reddit.com', 'github.com'}
AGENT = 'MetaMuseResearch/1.0'
MAX_BODY = 512 * 1024
RATE_BACKOFF = 900


def retry_deadline(headers, now, github):
    after = (headers.get('Retry-After') or '').strip()
    if after:
        if after.isascii() and after.isdecimal() and len(after) <= 12:
            return now + max(1, int(after))
        try:
            return max(now + 1, parsedate_to_datetime(after).timestamp())
        except (ValueError, OverflowError, TypeError, OSError):
            pass
    reset = headers.get('X-RateLimit-Reset', '') if github else ''
    if reset.isascii() and reset.isdecimal() and len(reset) <= 12 and int(reset) > now:
        return int(reset)
    return now + RATE_BACKOFF


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def source_url(value):
    if not isinstance(value, str) or len(value) > 700:
        raise Conflict('Bounded public source URL required')
    u = urlsplit(value)
    if (u.scheme != 'https' or u.netloc not in HOSTS or u.query or u.fragment or
            not re.fullmatch(r'/[A-Za-z0-9_./@+-]*', u.path) or '..' in u.path.split('/')):
        raise Conflict('Use a supported public HTTPS source without query parameters')
    path = u.path.rstrip('/') or '/'
    if u.netloc == 'github.com':
        match = re.fullmatch(r'/([\w.-]+/[\w.-]+)/(?:issues/(\d+)|releases(?:/(latest))?)', path)
        if match:
            repo, issue, latest = match.groups()
            resource = f'/issues/{issue}' if issue else '/releases' + ('/latest' if latest else '')
            return 'https://api.github.com/repos/' + repo + resource
        raise Conflict('Use a GitHub issue, Releases page, or latest release URL')
    if u.netloc == 'api.github.com' and not re.fullmatch(r'/repos/[\w.-]+/[\w.-]+/(?:issues/\d+|releases(?:/latest)?)', path):
        raise Conflict('Use a public repository issue or release endpoint')
    if u.netloc == 'www.reddit.com':
        if not re.fullmatch(r'/r/[\w]+/comments/[\w]+(?:/[\w-]+)?', path.removesuffix('/.rss')):
            raise Conflict('Use a public Reddit discussion URL')
        path = path.removesuffix('/.rss') + '/.rss'
    return 'https://' + u.netloc + path


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'nav', 'header', 'footer', 'svg', 'noscript'):
            self.skip.append(tag)

    def handle_endtag(self, tag):
        if self.skip and tag == self.skip[-1]:
            self.skip.pop()

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def extract(body, content_type):
    if content_type == 'application/json':
        value = json.loads(body)
        rows = value[:3] if isinstance(value, list) else [value]
        result = ' '.join(' '.join(str(row.get(k) or '') for k in
                                  ('title', 'name', 'tag_name', 'published_at', 'updated_at', 'body'))
                          for row in rows if isinstance(row, dict))
    elif content_type in ('text/html', 'application/atom+xml', 'application/rss+xml', 'application/xml', 'text/xml'):
        parser = VisibleText()
        parser.feed(body.decode('utf-8', 'replace'))
        result = ' '.join(parser.parts)
    elif content_type == 'text/plain':
        result = body.decode('utf-8', 'replace')
    else:
        raise Conflict('Unsupported source content type')
    result = ' '.join(result.split())
    if len(result) < 40:
        raise Conflict('Readable source text was too short')
    return result


class Research:
    def __init__(self, store, opener=None):
        self.store = store
        self.opener = opener or build_opener(NoRedirect)
        self.robots = {}
        self.lock = threading.Lock()

    def open(self, url, headers=None):
        return self.opener.open(Request(url, headers={'User-Agent': AGENT, **(headers or {})}), timeout=8)

    def allowed(self, url, now):
        host = urlsplit(url).netloc
        if host == 'api.github.com':
            return True
        checked, rules = self.robots.get(host, (0, None))
        if now - checked > 21600 or rules is None:
            rules = RobotFileParser()
            try:
                with self.open('https://' + host + '/robots.txt') as stream:
                    body = stream.read(65537)
                if len(body) > 65536:
                    return False
                rules.parse(body.decode('utf-8', 'replace').splitlines())
            except HTTPError as error:
                if error.code not in (404, 410):
                    return False
                rules.allow_all = True
            except (URLError, TimeoutError, OSError):
                return False
            self.robots[host] = (now, rules)
        return rules.can_fetch(AGENT, url)

    def get(self, supplied, min_age=900):
        url = source_url(supplied)
        identity = 'manager:' + hashlib.sha256(url.encode()).hexdigest()
        host = urlsplit(url).netloc
        limit_id = 'manager-source-backoff:' + host
        now = time.time()
        with self.lock:
            with self.store.lock:
                row = self.store.db.execute('SELECT state,last_attempt FROM source WHERE id=?', (identity,)).fetchone()
                limit = self.store.db.execute('SELECT data FROM setting WHERE id=?', (limit_id,)).fetchone()
            cached = json.loads(row['state']) if row and row['state'] else {}
            if row and 'retryAt' not in cached and now - row['last_attempt'] < min_age:
                return {**cached, 'change': 'cached'}
            if limit and (retry_at := json.loads(limit['data'])) > now:
                return self.rate_limited(url, now, retry_at)
            result = self.fetch(url, cached, now)
            with self.store.tx() as db:
                if 'retryAt' in result:
                    db.execute('INSERT INTO setting(id,data) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                               (limit_id, json.dumps(result['retryAt'])))
                db.execute('''INSERT INTO source(id,last_attempt,last_success,state) VALUES(?,?,?,?)
                              ON CONFLICT(id) DO UPDATE SET last_attempt=excluded.last_attempt,
                              last_success=COALESCE(excluded.last_success,source.last_success),state=excluded.state''',
                           (identity, now, now if result['availability'] == 'available' else None, json.dumps(result)))
            return result

    @staticmethod
    def rate_limited(url, now, retry_at):
        return {'url': url, 'availability': 'unavailable', 'change': 'unavailable', 'observedAt': now,
                'code': 'rate-limited', 'retryAt': retry_at,
                'reason': 'The public source host is rate limiting requests.'}

    def fetch(self, url, cached, now):
        failed = {'url': url, 'availability': 'unavailable', 'change': 'unavailable', 'observedAt': now}
        if not self.allowed(url, now):
            return {**failed, 'code': 'robots-policy', 'reason': 'Robots policy does not allow this fetch or could not be verified.'}
        headers = {}
        if cached.get('etag'):
            headers['If-None-Match'] = cached['etag']
        if cached.get('lastModified'):
            headers['If-Modified-Since'] = cached['lastModified']
        try:
            with self.open(url, headers) as response:
                body = response.read(MAX_BODY + 1)
                if len(body) > MAX_BODY:
                    return {**failed, 'code': 'response-too-large', 'reason': 'Response exceeds the public research size limit.'}
                content = extract(body, response.headers.get_content_type())
                source_hash = hashlib.sha256(content.encode()).hexdigest()
                return {'url': url, 'availability': 'available', 'observedAt': now,
                        'change': 'unchanged' if cached.get('sha256') == source_hash else 'changed',
                        'sha256': source_hash, 'excerpt': content[:3500],
                        'etag': response.headers.get('ETag'), 'lastModified': response.headers.get('Last-Modified')}
        except HTTPError as error:
            if error.code == 304 and cached.get('sha256'):
                return {**cached, 'availability': 'available', 'observedAt': now, 'change': 'unchanged'}
            github = urlsplit(url).netloc == 'api.github.com'
            headers = error.headers or {}
            if error.code == 429 or (github and error.code == 403 and
                                     (headers.get('X-RateLimit-Remaining') == '0' or headers.get('Retry-After'))):
                return self.rate_limited(url, now, retry_deadline(headers, now, github))
            return {**failed, 'code': 'http-error', 'reason': f'Public source returned HTTP {error.code}.'}
        except (TimeoutError, ValueError, OSError, Conflict):
            return {**failed, 'code': 'unreadable', 'reason': 'Public source could not be retrieved or parsed.'}
