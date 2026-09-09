"""Native ipykernel with bounded wire messages and no ambient startup configuration."""
import json
import os
from pathlib import Path
import zmq

FRAME_BYTES = 2 * 1024 * 1024
context = zmq.Context.instance()
context.setsockopt(zmq.SNDHWM, 16)
context.setsockopt(zmq.RCVHWM, 16)
context.setsockopt(zmq.MAXMSGSIZE, FRAME_BYTES)

from jupyter_client.session import Session
from ipykernel.kernelapp import IPKernelApp


class BoundedSession(Session):
    def send(self, stream, msg_or_type, content=None, parent=None, ident=None,
             buffers=None, track=False, header=None, metadata=None):
        message = (self.msg(msg_or_type, content=content, parent=parent,
                            header=header, metadata=metadata)
                   if isinstance(msg_or_type, str) else msg_or_type)
        # Detect large output before ZMQ's receive-side MAXMSGSIZE can discard it
        # without an observable error. The daemon independently bounds process RSS.
        size = sum(len(part) for part in self.serialize(message, ident=ident))
        size += sum(len(part) for part in (buffers or message.get('buffers', [])))
        if size > FRAME_BYTES - 1024:
            value = json.loads(Path(os.environ['SUPABRICKS_NOTEBOOK_CONTEXT']).read_text())
            fault = Path(value['fault'])
            temporary = fault.with_suffix('.tmp')
            temporary.write_text(json.dumps({'error': 'output_limit'}))
            temporary.replace(fault)
            os._exit(71)
        return super().send(stream, message, ident=ident, buffers=buffers, track=track)


app = IPKernelApp.instance()
app.session = BoundedSession(parent=app)
app.initialize()
app.start()
