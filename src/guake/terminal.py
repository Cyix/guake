# -*- coding: utf-8; -*-
"""
Copyright (C) 2007-2012 Lincoln de Sousa <lincoln@minaslivre.org>
Copyright (C) 2007 Gabriel Falcão <gabrielteratos@gmail.com>

This program is free software; you can redistribute it and/or
modify it under the terms of the GNU General Public License as
published by the Free Software Foundation; either version 2 of the
License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
General Public License for more details.

You should have received a copy of the GNU General Public
License along with this program; if not, write to the
Free Software Foundation, Inc., 51 Franklin Street, Fifth Floor,
Boston, MA 02110-1301 USA
"""

from __future__ import absolute_import
from __future__ import division

import gconf
import gtk
import logging
import os
import pango
import re
import subprocess
import vte

from guake.common import clamp
from guake.globals import KEY

__all__ = ["GuakeTerminalBox"]


# regular expressions to highlight links in terminal. This code was
# lovely stolen from the great gnome-terminal project, thank you =)
USERCHARS = "-[:alnum:]"
PASSCHARS = "-[:alnum:],?;.:/!%$^*&~\"#'"
HOSTCHARS = "-[:alnum:]"
HOST = "[" + HOSTCHARS + "]+(\\.[" + HOSTCHARS + "]+)*"
PORT = "(:[[:digit:]]{1,5})?"
PATHCHARS = "-[:alnum:]_$.+!*(),;:@&=?/~#%"
SCHEME = "(news:|telnet:|nntp:|file:/|https?:|ftps?:|webcal:)"
USER = "[" + USERCHARS + "]+(:[" + PASSCHARS + "]+)?"
URLPATH = "/[" + PATHCHARS + "]*[^]'.}>) \t\r\n,\\\"]"

TERMINAL_MATCH_EXPRS = [
    r"\<" + SCHEME + "//(" + USER + r"@)?" + HOST + PORT + "(" + URLPATH + r")?\>/?",
    r"\<(www|ftp)[" + HOSTCHARS + r"]*\." + HOST + PORT + "(" + URLPATH + r")?\>/?",
    r"\<(mailto:)?[" + USERCHARS + "][" + USERCHARS + ".]*@[" + HOSTCHARS +
    r"]+\." + HOST + r"\>"
]

TERMINAL_MATCH_TAGS = 'schema', 'http', 'email'

# tuple (title/quick matcher/filename and line number extractor)
QUICK_OPEN_MATCHERS = [
    ("Python traceback",
     r"^\s\sFile\s\".*\",\sline\s[0-9]+",
     r"^\s\sFile\s\"(.*)\",\sline\s([0-9]+)"),
    ("line starts by 'Filename:line' pattern (GCC/make). File path should exists.",
     r"^[a-zA-Z0-9\/\_\-\.\ ]+\.?[a-zA-Z0-9]+\:[0-9]+",
     r"^(.*)\:([0-9]+)")
]


class GuakeTerminal(vte.Terminal):

    """Just a vte.Terminal with some properties already set.
    """

    def __init__(self):
        super(GuakeTerminal, self).__init__()
        self.configure_terminal()
        self.add_matches()
        self.connect('button-press-event', self.button_press)
        self.matched_value = ''
        self.font_scale_index = 0
        self.pid = None
        self.custom_bgcolor = None
        self.custom_fgcolor = None

    def get_pid(self):
        return self.pid

    def configure_terminal(self):
        """Sets all customized properties on the terminal
        """
        client = gconf.client_get_default()
        word_chars = client.get_string(KEY('/general/word_chars'))
        self.set_word_chars(word_chars)
        self.set_audible_bell(client.get_bool(KEY('/general/use_audible_bell')))
        self.set_visible_bell(client.get_bool(KEY('/general/use_visible_bell')))
        self.set_sensitive(True)
        self.set_flags(gtk.CAN_DEFAULT)
        self.set_flags(gtk.CAN_FOCUS)
        cursor_blink_mode = client.get_int(KEY('/style/cursor_blink_mode'))
        client.set_int(KEY('/style/cursor_blink_mode'), cursor_blink_mode)
        cursor_shape = client.get_int(KEY('/style/cursor_shape'))
        client.set_int(KEY('/style/cursor_shape'), cursor_shape)

    def add_matches(self):
        """Adds all regular expressions declared in
        guake.globals.TERMINAL_MATCH_EXPRS to the terminal to make vte
        highlight text that matches them.
        """
        for expr in TERMINAL_MATCH_EXPRS:
            tag = self.match_add(expr)
            self.match_set_cursor_type(tag, gtk.gdk.HAND2)

        for _useless, match, _otheruseless in QUICK_OPEN_MATCHERS:
            tag = self.match_add(match)
            self.match_set_cursor_type(tag, gtk.gdk.HAND2)

    def get_current_directory(self):
        directory = os.path.expanduser('~')
        if self.pid is not None:
            cwd = os.readlink("/proc/%d/cwd" % self.pid)
            if os.path.exists(cwd):
                directory = cwd
        return directory

    def button_press(self, terminal, event):
        """Handles the button press event in the terminal widget. If
        any match string is caught, another application is open to
        handle the matched resource uri.
        """
        self.matched_value = ''
        matched_string = self.match_check(
            int(event.x / self.get_char_width()),
            int(event.y / self.get_char_height()))

        if (event.button == 1 and event.get_state() & gtk.gdk.CONTROL_MASK and
                matched_string):
            print "matched string:", matched_string
            value, tag = matched_string
            # First searching in additional matchers
            found = False
            client = gconf.client_get_default()
            use_quick_open = client.get_bool(KEY("/general/quick_open_enable"))
            quick_open_in_current_terminal = client.get_bool(
                KEY("/general/quick_open_in_current_terminal"))
            cmdline = client.get_string(KEY("/general/quick_open_command_line"))
            if use_quick_open:
                for _useless, _otheruseless, extractor in QUICK_OPEN_MATCHERS:
                    g = re.compile(extractor).match(value)
                    if g and len(g.groups()) == 2:
                        filename = g.group(1)
                        line_number = g.group(2)
                        filepath = filename
                        if not quick_open_in_current_terminal:
                            curdir = self.get_current_directory()
                            filepath = os.path.join(curdir, filename)
                            if not os.path.exists(filepath):
                                logging.info("Cannot open file %s, it doesn't exists locally"
                                             "(current dir: %s)", filepath,
                                             os.path.curdir)
                                continue
                        # for quick_open_in_current_terminal, we run the command line directly in
                        # the tab so relative path is enough.
                        #
                        # We do not test for file existence, because it doesn't work in ssh
                        # sessions.
                        logging.debug("Opening file %s at line %s", filepath, line_number)
                        resolved_cmdline = cmdline % {"file_path": filepath,
                                                      "line_number": line_number}
                        logging.debug("Command line: %s", resolved_cmdline)
                        if quick_open_in_current_terminal:
                            logging.debug("Executing it in current tab")
                            if resolved_cmdline[-1] != '\n':
                                resolved_cmdline += '\n'
                            self.feed_child(resolved_cmdline)
                        else:
                            logging.debug("Executing it independently")
                            subprocess.call(resolved_cmdline, shell=True)
                        found = True
                        break
            if not found:
                print "found tag:", tag
                print "found item:", value
                print "TERMINAL_MATCH_TAGS", TERMINAL_MATCH_TAGS
                if tag in TERMINAL_MATCH_TAGS:
                    if TERMINAL_MATCH_TAGS[tag] == 'schema':
                        # value here should not be changed, it is right and
                        # ready to be used.
                        pass
                    elif TERMINAL_MATCH_TAGS[tag] == 'http':
                        value = 'http://%s' % value
                    elif TERMINAL_MATCH_TAGS[tag] == 'https':
                        value = 'https://%s' % value
                    elif TERMINAL_MATCH_TAGS[tag] == 'ftp':
                        value = 'ftp://%s' % value
                    elif TERMINAL_MATCH_TAGS[tag] == 'email':
                        value = 'mailto:%s' % value

                if value:
                    cmd = ["xdg-open", value]
                    print "Opening link: {}".format(cmd)
                    subprocess.Popen(cmd, shell=False)
                    # gtk.show_uri(self.window.get_screen(), value,
                    #              gtk.gdk.x11_get_server_time(self.window))
        elif event.button == 3 and matched_string:
            self.matched_value = matched_string[0]

    def set_font(self, font):
        self.font = font
        self.set_font_scale_index(0)

    def set_font_scale_index(self, scale_index):
        self.font_scale_index = clamp(scale_index, -6, 12)

        font = pango.FontDescription(self.font.to_string())
        scale_factor = 2 ** (self.font_scale_index / 6)
        new_size = int(scale_factor * font.get_size())

        if font.get_size_is_absolute():
            font.set_absolute_size(new_size)
        else:
            font.set_size(new_size)

        super(GuakeTerminal, self).set_font(font)

    font_scale = property(
        fset=set_font_scale_index,
        fget=lambda self: self.font_scale_index
    )

    def increase_font_size(self):
        self.font_scale += 1

    def decrease_font_size(self):
        self.font_scale -= 1


class GuakeTerminalBox(gtk.HBox):

    """A box to group the terminal and a scrollbar.
    """

    def __init__(self):
        super(GuakeTerminalBox, self).__init__()
        self.terminal = GuakeTerminal()
        self.add_terminal()
        self.add_scroll_bar()

    def add_terminal(self):
        """Packs the terminal widget.
        """
        self.pack_start(self.terminal, True, True)
        self.terminal.show()

    def add_scroll_bar(self):
        """Packs the scrollbar.
        """
        adj = self.terminal.get_adjustment()
        scroll = gtk.VScrollbar(adj)
        scroll.set_no_show_all(True)
        self.pack_start(scroll, False, False)
