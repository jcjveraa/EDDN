# coding: utf8

"""
EDDN Gateway, which receives message from uploaders.

Contains the necessary ZeroMQ socket and a helper function to publish
market data to the Announcer daemons.
"""
import logging
import zlib
from datetime import datetime
from typing import Callable, Dict
from urllib.parse import parse_qs

import gevent
import simplejson
import zmq.green as zmq
from bottle import Bottle, request, response
from gevent import monkey
from pkg_resources import resource_string

from eddn.conf.Settings import Settings, load_config
from eddn.core.Validator import ValidationSeverity, Validator

monkey.patch_all()

app = Bottle()

logger = logging.getLogger(__name__)

# This socket is used to push market data out to the Announcers over ZeroMQ.
zmq_context = zmq.Context()
sender = zmq_context.socket(zmq.PUB)

validator = Validator()

# This import must be done post-monkey-patching!
from eddn.core.StatsCollector import StatsCollector  # noqa: E402

stats_collector = StatsCollector()
stats_collector.start()


def configure() -> None:
    """
    Get the list of transports to bind from settings.

    This allows us to PUB messages to multiple announcers over a variety of
    socket types (UNIX sockets and/or TCP sockets).
    """
    for binding in Settings.GATEWAY_SENDER_BINDINGS:
        sender.bind(binding)

    for schema_ref, schema_file in Settings.GATEWAY_JSON_SCHEMAS.iteritems():
        validator.addSchemaResource(schema_ref, resource_string('eddn.Gateway', schema_file))


def push_message(parsed_message: Dict, topic: str) -> None:
    """
    Push a message our to subscribed listeners.

    Spawned as a greenlet to push messages (strings) through ZeroMQ.
    This is a dumb method that just pushes strings; it assumes you've already
    validated and serialised as you want to.
    """
    string_message = simplejson.dumps(parsed_message, ensure_ascii=False).encode('utf-8')

    # Push a zlib compressed JSON representation of the message to
    # announcers with schema as topic
    compressed_msg = zlib.compress(string_message)

    send_message = f"{str(topic)!r} |-| {compressed_msg!r}"

    sender.send(send_message)
    stats_collector.tally("outbound")


def get_remote_address() -> str:
    """
    Determine the address of the uploading client.

    First checks the for proxy-forwarded headers, then falls back to
    request.remote_addr.
    :returns: Best attempt at remote address.
    """
    return request.headers.get('X-Forwarded-For', request.remote_addr)


def get_decompressed_message() -> bytes:
    """
    Detect gzip Content-Encoding headers and de-compress on the fly.

    For upload formats that support it.
    :rtype: str
    :returns: The de-compressed request body.
    """
    content_encoding = request.headers.get('Content-Encoding', '')

    if content_encoding in ['gzip', 'deflate']:
        # Compressed request. We have to decompress the body, then figure out
        # if it's form-encoded.
        try:
            # Auto header checking.
            message_body = zlib.decompress(request.body.read(), 15 + 32)

        except zlib.error:
            # Negative wbits suppresses adler32 checksumming.
            message_body = zlib.decompress(request.body.read(), -15)

        # At this point, we're not sure whether we're dealing with a straight
        # un-encoded POST body, or a form-encoded POST. Attempt to parse the
        # body. If it's not form-encoded, this will return an empty dict.
        form_enc_parsed = parse_qs(message_body)
        if form_enc_parsed:
            # This is a form-encoded POST. The value of the data attrib will
            # be the body we're looking for.
            try:
                message_body = form_enc_parsed[b'data'][0]

            except (KeyError, IndexError):
                raise MalformedUploadError(
                    "No 'data' POST key/value found. Check your POST key "
                    "name for spelling, and make sure you're passing a value."
                )
    else:
        # Uncompressed request. Bottle handles all of the parsing of the
        # POST key/vals, or un-encoded body.
        data_key = request.forms.get('data')
        if data_key:
            # This is a form-encoded POST. Support the silly people.
            message_body = data_key

        else:
            # This is a non form-encoded POST body.
            message_body = request.body.read()

    return message_body


def parse_and_error_handle(data: bytes) -> str:
    """
    Parse an incoming message and handle errors.

    :param data:
    :return: The decoded message, or an error message.
    """
    try:
        parsed_message = simplejson.loads(data)

    except (MalformedUploadError, TypeError, ValueError) as exc:
        # Something bad happened. We know this will return at least a
        # semi-useful error message, so do so.
        response.status = 400
        logger.error(f"Error to {get_remote_address()}: {exc}")
        return str(exc)

    # Here we check if an outdated schema has been passed
    if parsed_message["$schemaRef"] in Settings.GATEWAY_OUTDATED_SCHEMAS:
        response.status = '426 Upgrade Required'  # Bottle (and underlying httplib) don't know this one
        stats_collector.tally("outdated")
        return "FAIL: The schema you have used is no longer supported. Please check for an updated version of your " \
               "application."

    validation_results = validator.validate(parsed_message)

    if validation_results.severity <= ValidationSeverity.WARN:
        parsed_message['header']['gatewayTimestamp'] = datetime.utcnow().isoformat() + 'Z'
        parsed_message['header']['uploaderIP'] = get_remote_address()

        # Sends the parsed message to the Relay/Monitor as compressed JSON.
        gevent.spawn(push_message, parsed_message, parsed_message['$schemaRef'])
        logger.info(f"Accepted {parsed_message} upload from {get_remote_address()}")
        return 'OK'

    else:
        response.status = 400
        stats_collector.tally("invalid")
        return "FAIL: " + str(validation_results.messages)


@app.route('/upload/', method=['OPTIONS', 'POST'])
def upload() -> str:
    """
    Handle an /upload/ request.

    :return: The processed message, else error string.
    """
    try:
        # Body may or may not be compressed.
        message_body = get_decompressed_message()

    except zlib.error as exc:
        # Some languages and libs do a crap job zlib compressing stuff. Provide
        # at least some kind of feedback for them to try to get pointed in
        # the correct direction.
        response.status = 400
        logger.error(f"gzip error with {get_remote_address()}: {exc}")
        return str(exc)

    except MalformedUploadError as exc:
        # They probably sent an encoded POST, but got the key/val wrong.
        response.status = 400
        logger.error(f"Error to {get_remote_address()}: {exc}")
        return str(exc)

    stats_collector.tally("inbound")
    return parse_and_error_handle(message_body)


@app.route('/health_check/', method=['OPTIONS', 'GET'])
def health_check() -> str:
    """
    Return our version string in as an 'am I awake' signal.

    This should only be used by the gateway monitoring script. It is used
    to detect whether the gateway is still alive, and whether it should remain
    in the DNS rotation.

    :returns: Version of this software.
    """
    return Settings.EDDN_VERSION


@app.route('/stats/', method=['OPTIONS', 'GET'])
def stats() -> str:
    """
    Return some stats about the Gateway's operation so far.

    :return: JSON stats data
    """
    stats_current = stats_collector.getSummary()
    stats_current["version"] = Settings.EDDN_VERSION
    return simplejson.dumps(stats_current)


class MalformedUploadError(Exception):
    """
    Exception for malformed upload.

    Raise this when an upload is structurally incorrect. This isn't so much
    to do with something like a bogus region ID, this is more like "You are
    missing a POST key/val, or a body".
    """

    pass


class EnableCors(object):
    """Handle enabling CORS headers in all responses."""

    name = 'enable_cors'
    api = 2

    @staticmethod
    def apply(self, fn: Callable, context: str):
        """
        Apply CORS headers to the calling bottle app.

        :param fn:
        :param context:
        :return:
        """
        def _enable_cors(*args, **kwargs):
            # set CORS headers
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = \
                'Origin, Accept, Content-Type, X-Requested-With, X-CSRF-Token'

            if request.method != 'OPTIONS':
                # actual request; reply with the actual response
                return fn(*args, **kwargs)

        return _enable_cors


def main() -> None:
    """Handle setting up and running the bottle app."""
    load_config()
    configure()

    app.install(EnableCors())
    app.run(
        host=Settings.GATEWAY_HTTP_BIND_ADDRESS,
        port=Settings.GATEWAY_HTTP_PORT,
        server='gevent',
        certfile=Settings.CERT_FILE,
        keyfile=Settings.KEY_FILE
    )


if __name__ == '__main__':
    main()
