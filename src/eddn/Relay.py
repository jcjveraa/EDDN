# coding: utf8

"""EDDN Relay, which passes messages from the Gateway to listeners."""
import hashlib
import logging
import time
import uuid
import zlib
from threading import Thread

import gevent
import simplejson
import zmq.green as zmq
from bottle import Bottle, response
from gevent import monkey

from eddn.conf.Settings import Settings, load_config

monkey.patch_all()

app = Bottle()

# Logging has to be configured first before we do anything.
logger = logging.getLogger(__name__)

# This import must be done post-monkey-patching!
from eddn.core.StatsCollector import StatsCollector  # noqa: E402

stats_collector = StatsCollector()
stats_collector.start()

# This import must be done post-monkey-patching!
if Settings.RELAY_DUPLICATE_MAX_MINUTES:
    from eddn.core.DuplicateMessages import DuplicateMessages
    duplicate_messages = DuplicateMessages()
    duplicate_messages.start()


@app.route('/stats/', method=['OPTIONS', 'GET'])
def stats() -> str:
    """
    Return some stats about the Relay's operation so far.

    :return: JSON stats data
    """
    stats = stats_collector.get_summary()
    stats["version"] = Settings.EDDN_VERSION
    return simplejson.dumps(stats)


class Relay(Thread):
    """Relay thread class."""

    REGENERATE_UPLOADER_NONCE_INTERVAL = 12 * 60 * 60  # 12 hrs

    def __init__(self, **kwargs):
        super(Relay, self).__init__(**kwargs)
        self.uploader_nonce = None
        self.uploader_nonce_timestamp = 0
        self.generate_uploader_nonce()

    def generate_uploader_nonce(self) -> None:
        """Generate an uploader nonce."""
        self.uploader_nonce = str(uuid.uuid4())
        self.uploader_nonce_timestamp = time.time()

    def scramble_uploader(self, uploader: str) -> str:
        """
        Scramble an uploader ID.

        :param uploader: Plain text uploaderID.
        :return: Scrambled version of uploader.
        """
        now = time.time()
        if now - self.uploader_nonce_timestamp > self.REGENERATE_UPLOADER_NONCE_INTERVAL:
            self.generate_uploader_nonce()

        return hashlib.sha1(f"{self.uploader_nonce!r}-{uploader.encode}".encode('utf8')).hexdigest()

    def run(self) -> None:
        """Handle receiving messages from Gateway and passing them on."""
        # These form the connection to the Gateway daemon(s) upstream.
        context = zmq.Context()

        receiver = context.socket(zmq.SUB)
        receiver.setsockopt_string(zmq.SUBSCRIBE, '')

        for binding in Settings.RELAY_RECEIVER_BINDINGS:
            # Relays bind upstream to an Announcer, or another Relay.
            receiver.connect(binding)

        sender = context.socket(zmq.PUB)
        sender.setsockopt(zmq.SNDHWM, 500)

        for binding in Settings.RELAY_SENDER_BINDINGS:
            # End users, or other relays, may attach here.
            sender.bind(binding)

        def relay_worker(message: bytes) -> None:
            """
            Worker that resends messages to any subscribers.

            :param message: Message to be passed on.
            """
            message_text = zlib.decompress(message)
            json = simplejson.loads(message_text)

            # Handle duplicate message
            if Settings.RELAY_DUPLICATE_MAX_MINUTES:
                if duplicate_messages.is_duplicated(json):
                    # We've already seen this message recently. Discard it.
                    stats_collector.tally("duplicate")
                    return

            # Mask the uploader with a randomised nonce but still make it unique
            # for each uploader
            if 'uploaderID' in json['header']:
                json['header']['uploaderID'] = self.scramble_uploader(json['header']['uploaderID'])

            # Remove IP to end consumer
            if 'uploaderIP' in json['header']:
                del json['header']['uploaderIP']

            # Convert message back to JSON
            message_json = simplejson.dumps(json, sort_keys=True)

            # Recompress message
            message = zlib.compress(message_json.encode('utf8'))

            # Send message
            sender.send(message)
            stats_collector.tally("outbound")

        while True:
            # For each incoming message, spawn a greenlet using the relay_worker
            # function.
            inbound_message = receiver.recv()
            stats_collector.tally("inbound")
            gevent.spawn(relay_worker, inbound_message)


def apply_cors():
    """
    Apply a CORS handler.

    Ref: <https://stackoverflow.com/a/17262900>
    """
    response.set_header(
        'Access-Control-Allow-Origin',
        '*'
    )
    response.set_header(
        'Access-Control-Allow-Methods',
        'GET, POST, PUT, OPTIONS'
    )
    response.set_header(
        'Access-Control-Allow-Headers',
        'Origin, Accept, Content-Type, X-Requested-With, X-CSRF-Token'
    )


def main() -> None:
    """Handle setting up and running the bottle app."""
    load_config()
    r = Relay()
    r.start()

    app.add_hook('after_request', apply_cors)
    app.run(
        host=Settings.RELAY_HTTP_BIND_ADDRESS,
        port=Settings.RELAY_HTTP_PORT,
        server='gevent',
        certfile=Settings.CERT_FILE,
        keyfile=Settings.KEY_FILE
    )


if __name__ == '__main__':
    main()
