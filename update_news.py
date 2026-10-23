#!/usr/bin/env python3
"""Collect relevant RSS stories and publish a main edition plus extra flash cards."""
import html
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

OUT = Path(__file__).with_name('news.json')
QUERIES = {
    'italia': 'politica Italia governo Parlamento ultime notizie',
    'mondo': 'geopolitica esteri conflitti diplomazia ultime notizie',
    'economia': 'economia Italia prezzi consumi inflazione aziende',
    'ia': 'intelligenza artificiale OpenAI AI technology news',
    'gaming': 'Supercell Brawl Stars videogiochi aggiornamenti',
    'generale': 'notizie principali Italia mondo oggi',
}
KEYWORDS = {
    'italia': ['governo','parlamento','decreto','elezioni','ministro','politica','legge','italia','commissione'],
    'mondo': ['guerra','attacco','pace','negoziati','presidente','nato','ue','europa','cina','usa','iran','israele','ucraina','russia','protesta','sanzioni'],
    'economia': ['prezzi','inflazione','energia','bollette','consumi','economia','lavoro','stipendi','tassi','bce','imprese','mercato'],
    'ia': ['openai','intelligenza artificiale','artificial intelligence','chatgpt','modello','ai ','ia ','chip','tecnologia'],
    'gaming': ['supercell','brawl stars','clash royale','videogiochi','videogioco','gaming','aggiornamento','update'],
    'generale': ['ultime notizie','breaking','annuncia','annunciato','approvato','accordo','crisi','scoperta','lancio','nuovo'],
}
MEDIA_NS = {'media': 'http://search.yahoo.com/mrss/'}
TRUSTED_SOURCE_HINTS = (
    'ansa', 'agi', 'adnkronos', 'rai news', 'rainews', 'sky tg24', 'corriere della sera',
    'la repubblica', 'la stampa', 'il sole 24 ore', 'sole 24 ore', 'il post', 'internazionale',
    'il fatto quotidiano', 'open online', 'il messaggero', 'milano finanza', 'reuters',
    'associated press', 'ap news', 'bbc', 'france 24', 'deutsche welle', 'the guardian',
    'financial times', 'new york times', 'washington post', 'npr', 'al jazeera', 'abc news',
    'eurostat', 'commissione europea', 'european commission', 'parlamento europeo',
    'european parliament', 'banca centrale europea', 'european central bank', 'world health organization',
    'ministero', 'protezione civile', 'openai', 'anthropic', 'google deepmind', 'google research', 'microsoft', 'the verge',
    'ars technica', 'wired', 'techcrunch', 'mit technology review', 'supercell', 'ign', 'eurogamer',
    'polygon', 'video games chronicle', 'rock paper shotgun',
)
MIN_SCORE = 2
MAX_AGE_HOURS = 48
IMAGE_LOOKUP_LIMIT = 18
REQUEST_HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; NexaNewsPersonal/1.0; +https://news.google.com/)'}


class ImageParser(HTMLParser):
    """Read image URLs from an RSS description without executing its markup."""

    def __init__(self):
        super().__init__()
        self.sources = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'img':
            source = dict(attrs).get('src')
            if source:
                self.sources.append(source.strip())


class PageImageParser(HTMLParser):
    """Read Open Graph/Twitter image metadata from a publisher page."""

    def __init__(self):
        super().__init__()
        self.sources = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag.lower() == 'meta':
            key = (attrs.get('property') or attrs.get('name') or '').lower()
            if key in {'og:image', 'og:image:url', 'twitter:image', 'twitter:image:src'}:
                value = attrs.get('content')
                if value:
                    self.sources.append(value.strip())


def clean(value):
    value = html.unescape(value or '')
    value = re.sub(r'<[^>]+>', ' ', value)
    return re.sub(r'\s+', ' ', value).strip()


def parse_date(value):
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return date.astimezone(timezone.utc)
    except Exception:
        return None


def valid_image_url(value):
    try:
        if value.startswith('//'):
            value = 'https:' + value
        parts = urlsplit(value)
        return value if parts.scheme == 'https' and parts.netloc else ''
    except Exception:
        return ''


def trusted_source(source):
    normalized = re.sub(r'[^a-z0-9 ]+', ' ', source.lower())
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    return any(hint in normalized for hint in TRUSTED_SOURCE_HINTS)


def image_for(item, description):
    candidates = []
    for node in item.findall('.//media:thumbnail', MEDIA_NS):
        candidates.append(node.attrib.get('url', ''))
    for node in item.findall('.//media:content', MEDIA_NS):
        mime = node.attrib.get('type', '').lower()
        source = node.attrib.get('url', '')
        extension = urlsplit(source).path.lower().rsplit('.', 1)[-1] if '.' in urlsplit(source).path else ''
        if mime.startswith('image/') or (not mime and extension in {'avif', 'gif', 'jpeg', 'jpg', 'png', 'webp'}):
            candidates.append(source)
    for node in item.findall('./enclosure'):
        if node.attrib.get('type', '').lower().startswith('image/'):
            candidates.append(node.attrib.get('url', ''))
    parser = ImageParser()
    try:
        parser.feed(html.unescape(description or ''))
        candidates.extend(parser.sources)
    except Exception:
        pass
    return next((image for candidate in candidates if (image := valid_image_url(candidate))), '')


def publisher_image(url):
    """Try one lightweight publisher-page lookup when RSS has no image."""
    try:
        request = urllib.request.Request(url, headers=REQUEST_HEADERS)
        with urllib.request.urlopen(request, timeout=6) as response:
            content_type = response.headers.get('Content-Type', '').lower()
            if 'html' not in content_type:
                return ''
            page = response.read(1_000_000).decode('utf-8', errors='replace')
        parser = PageImageParser()
        parser.feed(page)
        base_url = response.geturl()
        for candidate in parser.sources:
            absolute = urllib.parse.urljoin(base_url, html.unescape(candidate))
            if image := valid_image_url(absolute):
                return image
    except Exception:
        pass
    return ''


def fetch(category, query):
    encoded_query = urllib.parse.quote(query)
    url = f'https://news.google.com/rss/search?q={encoded_query}+when:2d&hl=it&gl=IT&ceid=IT:it'
    request = urllib.request.Request(url, headers=REQUEST_HEADERS)
    with urllib.request.urlopen(request, timeout=20) as response:
        root = ET.fromstring(response.read())
    found = []
    for item in root.findall('./channel/item'):
        title = clean(item.findtext('title', ''))
        link = clean(item.findtext('link', ''))
        published = parse_date(item.findtext('pubDate', ''))
        if not title or not link or not published:
            continue
        age = datetime.now(timezone.utc) - published
        if age > timedelta(hours=MAX_AGE_HOURS) or age < timedelta(hours=-6):
            continue
        parts = title.rsplit(' - ', 1)
        source = parts[-1] if len(parts) > 1 else 'Google News'
        if not trusted_source(source):
            continue
        headline = parts[0] if len(parts) > 1 else title
        lower = headline.lower()
        score = sum(1 for word in KEYWORDS.get(category, []) if word in lower)
        if category == 'gaming' and any(word in lower for word in ['supercell', 'brawl stars', 'clash royale']):
            score += 3
        if category == 'ia' and any(word in lower for word in ['openai', 'chatgpt']):
            score += 2
        if category == 'generale':
            score += 1
        if score < MIN_SCORE:
            continue
        description = item.findtext('description', '')
        summary = clean(description)
        if not summary:
            summary = 'Apri la fonte originale per leggere il contesto completo.'
        summary = summary[:420].rsplit(' ', 1)[0] if len(summary) > 420 else summary
        found.append({
            'category': category,
            'title': headline,
            'summary': summary,
            'source': source,
            'url': link,
            'image_url': image_for(item, description),
            'published_at': published.isoformat(),
            'score': score,
        })
    return found


def main():
    all_items = []
    failed_categories = set()
    for category, query in QUERIES.items():
        try:
            all_items.extend(fetch(category, query))
        except Exception as error:
            failed_categories.add(category)
            print(f'Feed {category} non disponibile: {error}')

    previous = {}
    try:
        old = json.loads(OUT.read_text(encoding='utf-8'))
        for item in old.get('articles', []) + old.get('flash_cards', []):
            if item.get('url'):
                previous[item['url']] = item
                if item.get('category') in failed_categories:
                    try:
                        published = datetime.fromisoformat(item['published_at'].replace('Z', '+00:00'))
                        if published.tzinfo is None:
                            published = published.replace(tzinfo=timezone.utc)
                        if datetime.now(timezone.utc) - published <= timedelta(hours=MAX_AGE_HOURS):
                            all_items.append(dict(item))
                    except (KeyError, TypeError, ValueError):
                        pass
    except (OSError, ValueError, TypeError):
        pass

    unique = {}
    seen_urls = set()
    for item in sorted(all_items, key=lambda story: (story['score'], story['published_at']), reverse=True):
        key = re.sub(r'[^a-z0-9]+', '', item['title'].lower())[:100]
        if key and key not in unique and item['url'] not in seen_urls:
            unique[key] = item
            seen_urls.add(item['url'])
    candidates = list(unique.values())
    ranked = sorted(candidates, key=lambda story: (story['score'], story['published_at']), reverse=True)

    # Keep every relevant, trusted story returned by the feeds. Split the
    # ranked edition into main coverage and additional flash cards without
    # dropping results or repeating the same story in both sections.
    chosen = ranked[:10]
    flash_cards = ranked[10:]

    # Preserve a previously discovered image if a later RSS response omits it.
    for item in ranked:
        old_item = previous.get(item['url'], {})
        if not item['image_url']:
            item['image_url'] = old_item.get('image_url', '')

    # Rotate through stories missing an RSS image, so even large editions get
    # a metadata lookup without making one five-minute run take too long.
    pending_images = [item for item in ranked if not item['image_url']]
    if pending_images:
        interval = int(datetime.now(timezone.utc).timestamp() // 300)
        start = (interval * IMAGE_LOOKUP_LIMIT) % len(pending_images)
        batch = (pending_images[start:] + pending_images[:start])[:IMAGE_LOOKUP_LIMIT]
        for item in batch:
            item['image_url'] = publisher_image(item['url'])

    chosen.sort(key=lambda story: story['published_at'], reverse=True)
    flash_cards.sort(key=lambda story: story['published_at'], reverse=True)

    payload = {
        'updated_at': datetime.now(timezone.utc).isoformat(),
        'articles': chosen,
        'flash_cards': flash_cards,
        'note': 'Tutte le notizie pertinenti trovate nelle fonti affidabili sono incluse; le flash card raccolgono le notizie aggiuntive.',
    }
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + '\n'
    try:
        current = json.loads(OUT.read_text(encoding='utf-8'))
        # Avoid deploying identical editions every five minutes.
        current.pop('updated_at', None)
        comparable = dict(payload)
        comparable.pop('updated_at', None)
        if current == comparable:
            print(f'No visible news changes; kept {len(chosen)} main stories and {len(flash_cards)} flash cards.')
            return
    except (OSError, ValueError, TypeError):
        pass
    OUT.write_text(serialized, encoding='utf-8')
    print(f'Wrote {len(chosen)} main stories and {len(flash_cards)} flash cards to {OUT}')


if __name__ == '__main__':
    main()
