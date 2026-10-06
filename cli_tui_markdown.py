"""Terminal-safe Markdown as prompt-toolkit fragments, never ANSI output."""

from functools import lru_cache
from io import StringIO
import re

from markdown_it import MarkdownIt
from prompt_toolkit.utils import get_cwidth
from rich import box
from rich.console import Console
from rich.markdown import BlockQuote, CodeBlock, Heading, ListItem, Markdown, TableElement
from rich.padding import Padding
from rich.segment import Segment
from rich.style import Style
from rich.syntax import Syntax, SyntaxTheme
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from cli_tui_state import body_text
from cli_tui_theme import MOCHA


def wrap_plain(text, width):
    """Wrap by terminal cells, keeping words intact whenever they fit."""
    width = max(1, width)
    for line in body_text(text).split('\n'):
        part, used = '', 0
        for match in re.finditer(r'\s+|\S+', line):
            token = match.group()
            cells = get_cwidth(token)
            if cells <= width:
                if used + cells > width:
                    yield part.rstrip()
                    part, used = '', 0
                    if token.isspace():
                        continue
                part += token
                used += cells
                continue
            if part:
                yield part.rstrip()
                part, used = '', 0
            for char in token:
                cells = max(0, get_cwidth(char))
                if used + cells > width and part:
                    yield part
                    part, used = '', 0
                if cells <= width:
                    part += char
                    used += cells
        yield part


class _Heading(Heading):
    def __rich_console__(self, console, options):
        self.text.justify = 'left'
        yield self.text


class _CodeBlock(CodeBlock):
    def __rich_console__(self, console, options):
        # Keep padding outside Syntax: older Rich versions crop wrapped code
        # when Syntax's own padding is combined with wide Unicode characters.
        code = Syntax(str(self.text).rstrip('\n'), self.lexer_name, theme=self.theme,
                      word_wrap=True, padding=0)
        yield Padding(code, (0, 1), style=self.theme.get_background_style())


class _BlockQuote(BlockQuote):
    def __rich_console__(self, console, options):
        # Stop adding gutters before deeply nested quotes exhaust the panel.
        prefix = '│ ' if options.max_width >= 14 else ''
        child_options = options.update(width=options.max_width - len(prefix))
        for line in console.render_lines(self.elements, child_options, style=self.style, pad=False):
            yield Segment(prefix, self.style)
            yield from line
            yield Segment.line()


class _Table(TableElement):
    def __rich_console__(self, console, options):
        headings = [cell.content for cell in self.header.row.cells] if self.header and self.header.row else []
        rows = [[cell.content for cell in row.cells] for row in self.body.rows] if self.body else []
        if options.max_width < max(1, len(headings)) * 10:
            # Stacked rows keep every value readable on narrow panels.
            if not rows:
                yield from headings
            for index, row in enumerate(rows):
                if index:
                    yield Text('')
                for heading, cell in zip(headings, row):
                    yield Text.assemble(heading, ': ', cell, overflow='fold')
            return
        table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False,
                      collapse_padding=True, style='markdown.table.border')
        for heading in headings:
            heading = heading.copy()
            heading.stylize('markdown.table.header')
            table.add_column(heading, overflow='fold')
        for row in rows:
            table.add_row(*row)
        yield table


class _ListItem(ListItem):
    def _render_item(self, console, options, marker, style_name):
        prefix = marker if options.max_width - len(marker) >= 12 else ''
        child_options = options.update(width=options.max_width - len(prefix))
        style = console.get_style(style_name)
        for index, line in enumerate(console.render_lines(
                self.elements, child_options, style=self.style, pad=False)):
            yield Segment(prefix if index == 0 else ' ' * len(prefix), style)
            yield from line
            yield Segment.line()

    def render_bullet(self, console, options):
        yield from self._render_item(console, options, '• ', 'markdown.item.bullet')

    def render_number(self, console, options, number, last_number):
        marker = f'{number}. '.rjust(len(str(last_number)) + 2)
        yield from self._render_item(console, options, marker, 'markdown.item.number')


class _CodeTheme(SyntaxTheme):
    """Match the existing terminal palette instead of a second editor theme."""

    def get_style_for_token(self, token_type):
        name = str(token_type)
        for prefix, color in (
                ('Token.Comment', 'subtext0'), ('Token.Keyword', 'mauve'),
                ('Token.Literal.String', 'green'), ('Token.Literal.Number', 'peach'),
                ('Token.Name.Function', 'blue'), ('Token.Name.Class', 'yellow'),
                ('Token.Operator', 'sky'), ('Token.Error', 'red')):
            if name == prefix or name.startswith(prefix + '.'):
                return Style(color=MOCHA[color])
        return Style(color=MOCHA['text'])

    def get_background_style(self):
        return Style(bgcolor=MOCHA['mantle'])


class _Markdown(Markdown):
    elements = {**Markdown.elements, 'heading_open': _Heading,
                'fence': _CodeBlock, 'code_block': _CodeBlock,
                'blockquote_open': _BlockQuote, 'table_open': _Table,
                'list_item_open': _ListItem}


_PARSER = MarkdownIt('commonmark', {'html': False, 'maxNesting': 64}).enable(['table', 'strikethrough'])
_CONSOLE = Console(file=StringIO(), color_system='truecolor', force_terminal=True,
                   legacy_windows=False, markup=False, highlight=False, emoji=False,
                   theme=Theme({
                       **{f'markdown.h{i}': f'bold {MOCHA["lavender"]}' for i in range(1, 7)},
                       'markdown.strong': 'bold', 'markdown.em': 'italic',
                       'markdown.s': 'strike',
                       'markdown.code': f'{MOCHA["peach"]} on {MOCHA["mantle"]}',
                       'markdown.block_quote': MOCHA['subtext0'],
                       'markdown.link': f'underline {MOCHA["blue"]}',
                       'markdown.link_url': MOCHA['blue'],
                       'markdown.item.bullet': MOCHA['teal'],
                       'markdown.item.number': MOCHA['teal'],
                       'markdown.hr': MOCHA['surface2'],
                       'markdown.table.border': MOCHA['surface2'],
                       'markdown.table.header': f'bold {MOCHA["lavender"]}',
                   }))


@lru_cache(maxsize=512)
def _style(style):
    if style is None:
        return ''
    parts = []
    for color, prefix in ((style.color, 'fg:'), (style.bgcolor, 'bg:')):
        if color is not None and not color.is_default:
            parts.append(prefix + color.get_truecolor().hex)
    for name in ('bold', 'italic', 'underline', 'strike', 'reverse'):
        value = getattr(style, name)
        if value is not None:
            parts.append(name if value else 'no' + name)
    # Deliberately omit terminal hyperlinks and control sequences.
    return ' '.join(parts)


def _tokens(tokens):
    for token in tokens:
        yield token
        if token.children:
            yield from _tokens(token.children)


def _render(text, width):
    parsed = _PARSER.parse(text)
    flat = list(_tokens(parsed))
    if any(token.level >= 60 for token in flat):
        # Beyond the parser's nesting limit, raw source is preferable to loss.
        return tuple((('', line),) for line in wrap_plain(text, width))
    if ('\\' not in text and '&' not in text
            and all(token.type in ('paragraph_open', 'paragraph_close', 'inline', 'text', 'softbreak')
                    for token in flat)):
        # Plain diagnostics and command help retain alignment and blank lines.
        return tuple((('', line),) for line in wrap_plain(text, width))
    for token in flat:
        if token.type == 'softbreak':
            token.type = 'hardbreak'
        elif token.type == 'image':
            token.type, token.tag = 'text', ''
            token.content = f'Image: {token.content} ({token.attrGet("src") or ""})'
            token.children = None
    markdown = _Markdown('', code_theme=_CodeTheme(), hyperlinks=False)
    markdown.parsed = parsed
    lines = _CONSOLE.render_lines(markdown, _CONSOLE.options.update(width=width), pad=False)
    result = [tuple((_style(segment.style), body_text(segment.text))
                    for segment in line if not segment.control) for line in lines]
    while result and not ''.join(part[1] for part in result[-1]).strip():
        result.pop()
    return tuple(result) or ((('', ''),),)


@lru_cache(maxsize=64)
def _cached_render(text, width):
    return _render(text, width)


def markdown_lines(text, width, *, raw=False):
    """Immutable fragment rows, sharing the same raw source with F7 copy mode."""
    text, width = body_text(text), max(1, width)
    if raw or width < 12:
        return tuple((('', line),) for line in wrap_plain(text, width))
    # Do not retain unusually large messages or thousands of one-cell rows.
    if len(text) <= 16384 and width >= 20:
        return _cached_render(text, width)
    return _render(text, width)


def join_lines(lines):
    fragments = []
    for index, line in enumerate(lines):
        if index:
            fragments.append(('', '\n'))
        fragments.extend(line)
    return fragments
