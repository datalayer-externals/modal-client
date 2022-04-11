import asyncio
import codecs
import errno
import io
import os
import platform
import re
import typing
from asyncio import TimeoutError
from typing import Callable

from modal.config import logger
from modal_utils.async_utils import synchronizer


@synchronizer.asynccontextmanager
async def nullcapture(stream: io.IOBase):
    yield stream


def can_capture(stream: typing.Union[io.IOBase, typing.TextIO]):
    try:
        stream.fileno()
    except io.UnsupportedOperation:
        return False
    return True


@synchronizer.asynccontextmanager
async def thread_capture(stream: io.IOBase, callback: Callable[[str, io.TextIOBase], None]):
    """Intercept writes on a stream (typically stderr or stdout)"""
    fd = stream.fileno()
    dup_fd = os.dup(fd)
    orig_writer = os.fdopen(dup_fd, "w")

    if platform.system() != "Windows" and stream.isatty():
        import pty

        read_fd, write_fd = pty.openpty()
    else:
        # pty doesn't work on Windows.
        # TODO: this branch has not been tested.
        read_fd, write_fd = os.pipe()

    os.dup2(write_fd, fd)

    decoder = codecs.getincrementaldecoder("utf8")()

    def capture_thread():
        buf = ""

        while 1:
            try:
                raw_data = os.read(read_fd, 50)
            except OSError as err:
                if err.errno == errno.EIO:
                    # Input/Output error - triggered on linux when the write pipe is closed
                    raw_data = b""
                else:
                    raise

            if not raw_data:
                if buf:
                    callback(buf, orig_writer)
                return
            data = decoder.decode(raw_data)

            # Only send back lines that end in \n or \r.
            # This is needed to make progress bars and the like work well.
            # TODO: maybe write a custom IncrementalDecoder?
            # TODO: pty turns all \n into \r\n. On rare occasions, if the buffering separates the
            # \r and \n into separate lines, and there are concurrent prints happening, this can cause
            # some lines to be overwritten.
            chunks = re.split("(\r\n|\r|\n)", buf + data)

            # re.split("(<exp>)") returns the matched groups, and also the separators.
            # e.g. re.split("(+)", "a+b") returns ["a", "+", "b"].
            # This means that chunks is guaranteed to be odd in length.
            for i in range(int(len(chunks) / 2)):
                # piece together chunk back with separator.
                line = chunks[2 * i] + chunks[2 * i + 1]
                callback(line, orig_writer)

            buf = chunks[-1]

    # start thread but don't await it
    print_task = asyncio.get_event_loop().run_in_executor(None, capture_thread)
    try:
        yield orig_writer
    finally:
        stream.flush()  # flush any remaining writes on fake output
        os.close(write_fd)  # this should trigger eof in the capture thread
        os.dup2(dup_fd, fd)  # restore stdout
        try:
            await asyncio.wait_for(print_task, 3)  # wait for thread to empty the read buffer
        except TimeoutError:
            # TODO: this doesn't actually kill the thread, but since the pipe is closed it shouldn't
            #       capture more user output and eventually end when eof is reached on the read pipe
            logger.warn("Could not empty user output buffer. Some user output might be missing at this time")
        os.close(read_fd)
