# ---------------------------------------------------------------------------
# Console logger for MTDA
# ---------------------------------------------------------------------------
#
# This software is a part of MTDA.
# Copyright (c) Mentor, a Siemens business, 2017-2020
#
# ---------------------------------------------------------------------------
# SPDX-License-Identifier: MIT
# ---------------------------------------------------------------------------

# System imports
import codecs
from collections import deque
import os
import sys
import threading
import time


class ConsoleLogger:

    def __init__(self, mtda, console, socket=None, power_controller=None):
        self.mtda = mtda
        self.console = console
        self._prompt = "=> "
        self.power_controller = power_controller
        self.rx_alive = False
        self.rx_thread = None
        self.rx_queue = bytearray()
        self.rx_buffer = deque(maxlen=1000)
        self.rx_lock = threading.Lock()
        self.rx_cond = threading.Condition(self.rx_lock)
        self.socket = socket
        self.basetime = 0
        self.timestamps = False

    def start(self):
        self.rx_alive = True
        self.rx_thread = threading.Thread(
            target=self.reader, name='console_rx')
        self.rx_thread.daemon = True
        self.rx_thread.start()

    def _clear(self):
        self.rx_buffer.clear()
        self.rx_queue = bytearray()

    def clear(self):
        with self.rx_lock:
            self._clear()

    def _dump(self, flush=True):
        data = ""
        lines = len(self.rx_buffer)
        while lines > 0:
            line = self.rx_buffer.popleft().decode("utf-8", "ignore")
            data = data + line
            lines = lines - 1
        line = self.rx_queue.decode("utf-8", "ignore")
        data = data + line
        if flush:
            self.rx_queue = bytearray()
        return data

    def dump(self):
        with self.rx_lock:
            data = self._dump(flush=False)
        return data

    def flush(self):
        with self.rx_lock:
            data = self._dump(flush=True)
        return data

    def _head(self):
        if len(self.rx_buffer) > 0:
            line = self.rx_buffer.popleft().decode("utf-8", "ignore")
        else:
            line = None
        return line

    def head(self):
        with self.rx_lock:
            line = self._head()
        return line

    def lines(self):
        with self.rx_lock:
            lines = len(self.rx_buffer)
        return lines

    def _matchprompt(self):
        prompt = self._tail(False)
        if prompt is None:
            return False
        if prompt.startswith("\r"):
            prompt = prompt[1:]
        return prompt.endswith(self._prompt)

    def prompt(self, newPrompt=None):
        with self.rx_lock:
            if newPrompt is not None:
                self._prompt = newPrompt
            p = self._prompt
        return p

    def run(self, cmd):
        self.rx_lock.acquire()
        self._clear()

        # Send a break to get a prompt
        self.write("\3")

        # Wait for a prompt
        self.rx_cond.wait_for(self._matchprompt)

        # Send requested command
        self._clear()
        self.write("%s\n" % (cmd))

        # Wait for the command to complete
        self.rx_cond.wait_for(self._matchprompt)

        # Strip first line (command we sent) and flush received bytes
        self._head()
        data = self._dump(flush=True)

        # Release and return command output
        self.rx_lock.release()
        return data

    def _tail(self, discard=True):
        if len(self.rx_queue) > 0:
            line = self.rx_queue
        elif len(self.rx_buffer) > 0:
            line = self.rx_buffer[-1]
        else:
            return None
        if discard is True:
            self._clear()
        return line.decode("utf-8", "ignore")

    def tail(self):
        with self.rx_lock:
            line = self._tail()
        return line

    def write(self, data, raw=False):
        try:
            if raw is False:
                data = codecs.escape_decode(bytes(data, "utf-8"))[0]
            else:
                data = bytes(data, "utf-8")
            self.console.write(data)
        except IOError as e:
            print("write error on the console ({0})!".format(
                e.strerror), file=sys.stderr)

    def reset_timer(self):
        self.basetime = 0

    def toggle_timestamps(self):
        self.timestamps = not self.timestamps

    # Print bytes to the console (local or remote)
    def _print(self, data):
        if self.socket is not None:
            self.socket.send(data)
        else:
            # Write to stdout if received are not pushed to the network
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    # Print a string to the console (local or remote)
    def print(self, data):
        data = codecs.escape_decode(bytes(data, "utf-8"))[0]
        self._print(data)

    def process_rx(self, data):
        # Initialize basetime on the 1st byte we receive
        if not self.basetime:
            self.basetime = time.time()

        # Add timestamps
        if self.timestamps is True:
            newdata = bytearray()
            linefeeds = 0
            for x in data:
                if x == 0xd:
                    continue
                newdata.append(x)
                if x == 0xa:
                    linetime = time.time()
                    elapsed = linetime - self.basetime
                    timestr = "[%4.6f] " % elapsed
                    newdata.extend(timestr.encode("utf-8"))
                    linefeeds = linefeeds + 1
            data = newdata
        else:
            linefeeds = 1

        # Publish received data
        self._print(data)

        # Prevent concurrent access to the RX buffers
        self.rx_lock.acquire()

        # Add received data
        self.rx_queue.extend(data)

        # Find lines we have in the queue
        while linefeeds > 0:
            sz = len(self.rx_queue)
            off = self.rx_queue.find(b'\n', 0)
            if off >= 0:
                # Will include the line feed character in the buffered line
                off = off + 1
                rem = sz - off

                # Extract line from the RX queue
                line = self.rx_queue[:off]

                # Strip trailing \r
                if len(line) > 1 and line[-2] == 0xd and line[-1] == 0xa:
                    del line[-1:]
                    line[-1] = 0xa

                # Add this line to the circular buffer
                self.rx_buffer.append(line)

                # Remove consumed bytes from the queue
                if rem > 0:
                    self.rx_queue = self.rx_queue[-rem:]
                else:
                    self.rx_queue = bytearray()
            else:
                linefeeds = 0

        # Notify threads waiting on data
        self.rx_cond.notify()

        # Release access to the RX buffers
        self.rx_lock.release()

    def reader(self):
        con = self.console
        error = None
        retries = 3

        if self.power_controller is not None:
            self.power_controller.wait()
            with self.rx_lock:
                con.open()

        while self.rx_alive is True:
            if self.power_controller is not None:
                self.power_controller.wait()

            try:
                data = con.read(con.pending() or 1)
                self.process_rx(data)
                continue
            except IOError:
                error = sys.exc_info()[0]

            try:
                if retries > 0:
                    print("resetting console to recover from read error "
                          "({0})...".format(error), file=sys.stderr)
                    with self.rx_lock:
                        con.close()
                        con.open()
                    error = None
                else:
                    print("failed to reset the console, "
                          "aborting!", file=sys.stderr)
                    self.rx_alive = False
            except IOError:
                retries = retries - 1

    def pause(self):
        with self.rx_lock:
            self.console.close()

    def resume(self):
        with self.rx_lock:
            self.console.open()
