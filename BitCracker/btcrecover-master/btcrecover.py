#!/usr/bin/env python

# btcrecover.py -- Bitcoin wallet password recovery tool
# Copyright (C) 2014-2017 Christopher Gurnee
#
# This program is free software: you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version
# 2 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see http://www.gnu.org/licenses/

# If you find this program helpful, please consider a small
# donation to the developer at the following Bitcoin address:
#
#           3Au8ZodNHPei7MQiSVAWb7NB2yqsb48GW4
#
#                      Thank You!

# PYTHON_ARGCOMPLETE_OK - enables optional bash tab completion

from __future__ import print_function

from btcrecover import btcrpass
import sys, os, multiprocessing


def write_found_password(password):
    """Write the recovered password to a file instead of the terminal, so it
    doesn't persist in scrollback, tmux/screen logs, or redirected output.
    The file is created owner-read/write only where the OS supports it."""
    path = "RECOVERED_PASSWORD.txt"
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    f = os.fdopen(fd, "wb")
    try:
        f.write(password.encode("utf-8", "replace") if isinstance(password, unicode) else password)
    finally:
        f.close()
    print("Password found - written to " + path)


if __name__ == "__main__":

    print("Starting", btcrpass.full_version(),
          file=sys.stderr if any(a.startswith("--listp") for a in sys.argv[1:]) else sys.stdout)  # --listpass
    btcrpass.parse_arguments(sys.argv[1:])
    (password_found, not_found_msg) = btcrpass.main()

    if isinstance(password_found, basestring):
        write_found_password(password_found)
        retval = 0

    elif not_found_msg:
        print(not_found_msg, file=sys.stderr if btcrpass.args.listpass else sys.stdout)
        retval = 0

    else:
        retval = 1  # An error occurred or Ctrl-C was pressed

    # Wait for any remaining child processes to exit cleanly (to avoid error messages from gc)
    for process in multiprocessing.active_children():
        process.join(1.0)

    sys.exit(retval)
