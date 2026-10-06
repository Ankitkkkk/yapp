"""Catppuccin Mocha surfaces and terminal color capability selection."""

import os

from prompt_toolkit.output import ColorDepth
from prompt_toolkit.styles import Style


# Official palette: https://github.com/catppuccin/palette
MOCHA = {
    'base': '#1e1e2e', 'mantle': '#181825', 'crust': '#11111b',
    'surface0': '#313244', 'surface1': '#45475a', 'surface2': '#585b70',
    'overlay0': '#6c7086', 'subtext0': '#a6adc8', 'subtext1': '#bac2de',
    'text': '#cdd6f4', 'lavender': '#b4befe', 'mauve': '#cba6f7',
    'blue': '#89b4fa', 'sky': '#89dceb', 'teal': '#94e2d5',
    'green': '#a6e3a1', 'yellow': '#f9e2af', 'peach': '#fab387', 'red': '#f38ba8',
}


def _colors(foreground, background=None, extra=''):
    return ' '.join(filter(None, [MOCHA[foreground],
        'bg:' + MOCHA[background] if background else '', extra]))


TUI_STYLE = Style.from_dict({
    '': _colors('text', 'base'),
    'app': _colors('text', 'base'),
    'pane': _colors('text', 'base'),
    'pane.navigation': _colors('subtext1', 'mantle'),
    'pane.agents': _colors('text', 'mantle'),
    'pane.pending_inputs': _colors('subtext1', 'mantle'),
    'pane.composer': _colors('text', 'mantle'),
    'pane.composer text-area': _colors('text', 'mantle'),
    'frame.border': _colors('surface1'),
    'frame.label': _colors('subtext1', extra='bold'),
    'focused frame.border': _colors('lavender'),
    'focused frame.label': _colors('lavender', extra='bold'),
    'appbar': _colors('subtext1', 'mantle'),
    'appbar.brand': _colors('crust', 'mauve', 'bold'),
    'appbar.session': _colors('text', extra='bold'),
    'appbar.connected': _colors('green'),
    'appbar.connecting': _colors('yellow'),
    'footer': _colors('subtext1', 'mantle'),
    'footer.key': _colors('lavender', extra='bold'),
    'notice': _colors('subtext0', 'base'),
    'selected': _colors('lavender', 'surface0', 'bold noreverse'),
    'mode.normal': _colors('crust', 'lavender', 'bold noreverse'),
    'mode.insert': _colors('crust', 'green', 'bold noreverse'),
    'mode.copy': _colors('crust', 'yellow', 'bold noreverse'),
    'status.running': _colors('green'), 'status.starting': _colors('yellow'),
    'status.failed': _colors('red'), 'muted': _colors('subtext0'),
    'chat.you': _colors('mauve', extra='bold'),
    'chat.agent': _colors('teal', extra='bold'),
    'chat.time': _colors('subtext0'), 'chat.gutter': _colors('surface2'),
    'chat.attachment': _colors('blue', extra='underline'),
    'chat.choices': _colors('yellow'),
    'composer.mention': _colors('sky', extra='bold'),
    'agent.ready': _colors('green'), 'agent.working': _colors('blue'),
    'agent.input': _colors('yellow', extra='bold'), 'agent.starting': _colors('peach'),
    'agent.stopped': _colors('subtext0'), 'agent.error': _colors('red', extra='bold'),
    'agent.unread': _colors('mauve', extra='bold'),
    'session.current': _colors('blue', extra='bold'),
    'session.selected': _colors('lavender', 'surface0', 'bold noreverse'),
    'button': _colors('subtext1', 'surface0', 'noreverse'),
    'button.arrow': _colors('subtext0'),
    'button.focused': _colors('crust', 'lavender', 'bold noreverse'),
    'button.focused button.text': _colors('crust', 'lavender', 'bold noreverse'),
    'button.focused button.arrow': _colors('crust', 'lavender', 'bold noreverse'),
    'agent.review button': _colors('crust', 'yellow', 'bold'),
    'agent.review button.arrow': _colors('crust'),
    'agent.review button.focused': _colors('crust', 'peach', 'bold'),
    'agent.review button.focused button.text': _colors('crust', 'peach', 'bold'),
    'agent.review button.focused button.arrow': _colors('crust', 'peach', 'bold'),
    'pane-action button': _colors('subtext1', 'mantle', 'noreverse'),
    'pane-action button.focused': _colors('crust', 'lavender', 'bold noreverse'),
    'pane-action button.focused button.text': _colors('crust', 'lavender', 'bold noreverse'),
    'pane-action button.focused button.arrow': _colors('crust', 'lavender', 'bold noreverse'),
    'agent.review pane-action button': _colors('yellow', 'mantle', 'bold noreverse'),
    'agent.review pane-action button.focused': _colors('crust', 'yellow', 'bold noreverse'),
    'agent.review pane-action button.focused button.text': _colors('crust', 'yellow', 'bold noreverse'),
    'agent.review pane-action button.focused button.arrow': _colors('crust', 'yellow', 'bold noreverse'),
    'dialog': _colors('text', 'base'),
    'dialog.body': _colors('text', 'base'),
    'dialog frame.label': _colors('lavender', 'base', 'bold'),
    'dialog.body frame.label': _colors('lavender', 'base', 'bold'),
    'dialog.body frame.border': _colors('lavender', 'base'),
    'dialog.body text-area': _colors('text', 'mantle'),
    'dialog.body text-area last-line': 'nounderline',
    'dialog.body dialog.input': _colors('text', 'mantle'),
    'dialog.body dialog.input.focused': _colors('text', 'surface0'),
    'dialog.body dialog.input.readonly': _colors('subtext0', 'mantle'),
    'dialog.choice.selected': _colors('lavender', 'surface0', 'bold noreverse'),
    'dialog.error': _colors('red', extra='bold'),
    'radio-selected': _colors('lavender', 'surface0', 'bold noreverse'),
    'radio-checked': _colors('green', extra='bold'),
    'shadow': 'bg:' + MOCHA['crust'],
    'dialog shadow': 'bg:' + MOCHA['crust'],
    'dialog.body shadow': 'bg:' + MOCHA['crust'],
    'completion-menu': _colors('text', 'surface0'),
    'completion-menu.completion': _colors('text', 'surface0'),
    'completion-menu.completion.current': _colors('crust', 'lavender', 'bold noreverse'),
    'completion-menu.meta.completion': _colors('subtext1', 'mantle'),
    'completion-menu.meta.completion.current': _colors('text', 'surface1', 'noreverse'),
    'scrollbar.background': 'bg:' + MOCHA['surface0'],
    'scrollbar.button': 'bg:' + MOCHA['surface2'],
    'scrollbar.arrow': _colors('subtext0', 'surface0', 'bold'),
    'dialog.body scrollbar.button': 'bg:' + MOCHA['surface2'],
    'search': _colors('crust', 'yellow', 'noreverse'),
    'search.current': _colors('crust', 'peach', 'noreverse'),
    'selected-text': _colors('text', 'surface2', 'noreverse'),
})


def terminal_color_depth():
    """Honor explicit overrides; enable RGB where the terminal advertises it."""
    override = ColorDepth.from_env()
    if override is not None:
        return override
    if (os.environ.get('COLORTERM', '').lower() in ('truecolor', '24bit')
            or os.environ.get('TERM') == 'xterm-kitty'):
        return ColorDepth.DEPTH_24_BIT
    return None
