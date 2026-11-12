"""A fake heat pump behind a pseudo-terminal, so the *real* ``SerialTransport``
(pyserial, reader thread, reconnect handling) can be tested without hardware.

``plug()`` creates the device node at ``link``; ``unplug()`` removes it the way
pulling a USB adapter does.
"""

from __future__ import annotations

import os
import pty
import select
import threading
import tty

from stiebel_heatpump.simulator import HeatPumpSimulator


class FakePump:
    def __init__(self, config, link: str, **sim_kwargs) -> None:
        self._config = config
        self._link = link
        self._sim_kwargs = sim_kwargs
        self._master = self._slave = -1
        self._alive = False
        self._thread = None

    def plug(self) -> None:
        self._master, self._slave = pty.openpty()
        tty.setraw(self._slave)
        self.simulator = HeatPumpSimulator(self._config, **self._sim_kwargs)
        if os.path.islink(self._link):
            os.unlink(self._link)
        os.symlink(os.ttyname(self._slave), self._link)
        self._alive = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        master = self._master
        while self._alive:
            try:
                ready, _, _ = select.select([master], [], [], 0.05)
                if ready:
                    reply = self.simulator.feed(os.read(master, 256))
                    if reply:
                        os.write(master, reply)
            except OSError:
                return

    def unplug(self) -> None:
        self._alive = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        for fd in (self._master, self._slave):
            try:
                os.close(fd)
            except OSError:
                pass
        if os.path.islink(self._link):
            os.unlink(self._link)
