import sys
import threading
import time
from queue import Queue

from thonny.plugins.micropython.connection import MicroPythonConnection

DEBUG = False


class WebReplConnection(MicroPythonConnection):
    """
    Problem with block size:
    https://github.com/micropython/micropython/issues/2497
    Start with conservative delay.
    Client may later reduce it for better efficiency
    """

    def __init__(self, url, password, write_block_delay=0.5):

        self.num_bytes_received = 0
        super().__init__()

        try:
            import websockets  # @UnusedImport
        except:
            raise RuntimeError(
                "Can't import `websockets`. You can install it via 'Tools => Manage plug-ins'."
            )
        self._url = url
        self._password = password
        self._write_block_size = 255
        self._write_block_delay = write_block_delay
        self._write_responses = Queue()

        # Some tricks are needed to use async library in a sync program.
        # Useing thread-safe queues to communicate with async world in another thread
        self._write_queue = Queue()
        self._connection_result = Queue()
        self._ws_thread = threading.Thread(target=self._wrap_ws_main, daemon=True)
        self._ws_thread.start()

        # Wait until connection was made
        res = self._connection_result.get()
        if res != "OK":
            raise res

    def _wrap_ws_main(self):
        import asyncio

        loop = asyncio.new_event_loop()
        loop.set_debug(DEBUG)
        loop.run_until_complete(self._ws_main())

    async def _ws_main(self):
        import asyncio

        try:
            await self._ws_connect()
        except Exception as e:
            self._connection_result.put_nowait(e)
            return

        self._connection_result.put_nowait("OK")
        await asyncio.gather(self._ws_keep_reading(), self._ws_keep_writing())

    async def _ws_connect(self):
        import websockets

        try:
            self._ws = await websockets.connect(self._url, ping_interval=None)
        except websockets.exceptions.InvalidMessage:
            # try once more
            self._ws = await websockets.connect(self._url, ping_interval=None)

        debug("GOT WS", self._ws)

        # read password prompt and send password
        read_chars = ""
        while read_chars != "Password: ":
            debug("prelude", read_chars)
            ch = await self._ws.recv()
            debug("GOT", ch)
            read_chars += ch

        debug("sending password")
        await self._ws.send(self._password + "\n")
        debug("sent password")

    async def _ws_keep_reading(self):
        while True:
            data = (await self._ws.recv()).encode("UTF-8")
            if len(data) == 0:
                self._error = "EOF"
                break

            self.num_bytes_received += len(data)
            self._make_output_available(data, block=False)

    async def _ws_keep_writing(self):
        import asyncio

        while True:
            while not self._write_queue.empty():
                data = self._write_queue.get(block=False)
                debug(
                    "To be written:",
                    len(data),
                    self._write_block_size,
                    self._write_block_delay,
                    repr(data),
                )

                # chunk without breaking utf-8 chars
                start_pos = 0
                while start_pos < len(data):
                    if start_pos > 0:
                        await asyncio.sleep(self._write_block_delay)

                    end_pos = start_pos + self._write_block_size
                    # make sure next block doesn't start with a continuation char
                    while end_pos < len(data) and data[end_pos] >= 0x80 and data[end_pos] < 0xC0:
                        end_pos -= 1

                    block = data[start_pos:end_pos]
                    str_block = block.decode("UTF-8")
                    await self._ws.send(str_block)
                    debug("Wrote chars", len(str_block))

                    start_pos = end_pos

                debug("Wrote bytes", len(data))
                self._write_responses.put(len(data))

            # Allow reading loop to progress
            await asyncio.sleep(0.01)

    def write(self, data):
        self._write_queue.put_nowait(data)
        return self._write_responses.get()

    async def _async_close(self):
        await self._ws.close()

    def close(self):
        """
        import asyncio
        asyncio.get_event_loop().run_until_complete(self.async_close())
        """


def debug(*args):
    if DEBUG:
        print(*args, file=sys.stderr)
