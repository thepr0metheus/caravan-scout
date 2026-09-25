"""Who this machine is on the board: an id that stays, a name that follows it."""
from __future__ import annotations

import socket


class HostIdentity:
    """The id this machine goes by on the board, and the name the board shows.

    The id was the hostname, read at every start. A machine renamed with
    `hostnamectl` came back as a new host, and everything the board keeps
    under the old id stayed with a machine that no longer reported: its
    cells with their schedules and autostart, its power schedule, the client
    record of the same machine. Now the id in use is pinned in state.json,
    and at the next start the pin wins over the hostname: a rename changes
    only the name the board shows. The first run after an upgrade pins the
    id the scout reports now, so no machine turns into a new host by
    upgrading.

    The operator can still choose an id: hostId in config.json wins over the
    pin, and becomes the pin. The board then sees a new host, and its cells
    move with "move cells" on the board.

    Not /etc/machine-id: clones of one VM image share it, macOS has none,
    and it would make the controller match records by a second key.
    """

    #: The word for a machine that has no name at all.
    FALLBACK = "remote"

    def __init__(self, config, state):
        self.config = config
        self.state = state

    @staticmethod
    def hostname() -> str:
        """The machine's short name, as the kernel says it right now."""
        return socket.gethostname().split(".")[0].strip()

    def settle(self) -> str:
        """The id for this run — config.json's hostId, else the pin, else
        the hostname — pinned in state.json and put into the running config,
        which every reader of hostId reads."""
        chosen = str(self.config.from_file("hostId") or "").strip()
        with self.state.lock:
            pinned = str(self.state.get("hostId") or "").strip()
            host_id = chosen or pinned or self.hostname()
            if not host_id:
                # A machine with no name yet (an early boot): called something
                # for this run, pinned as nothing — the next start pins its name.
                self.config.data["hostId"] = self.FALLBACK
                return self.FALLBACK
            if host_id != pinned:
                self.state["hostId"] = host_id
                self.state.save()
        self.config.data["hostId"] = host_id
        if pinned and host_id != pinned:
            print(f"[identity] host id {pinned!r} -> {host_id!r}: config.json names it")
        elif not pinned:
            print(f"[identity] host id {host_id!r} pinned: a rename of this machine keeps it")
        return host_id

    def name(self) -> str:
        """The name the board shows: config.json's displayName, else the
        hostname right now — a rename shows at the next report, not at the
        next start of the scout."""
        return (str(self.config.from_file("displayName") or "").strip() or self.hostname()
                or str(self.config.get("hostId") or "") or self.FALLBACK)
