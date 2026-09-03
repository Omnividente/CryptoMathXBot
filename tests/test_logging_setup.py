import logging
import sys

from cryptomathxbot.logging_setup import _RedactingFormatter


def test_formatter_redacts_secret_from_message_and_traceback() -> None:
    secret = "123456:secret-token-value"
    try:
        raise RuntimeError(f"rejected {secret}")
    except RuntimeError:
        record = logging.LogRecord(
            "test",
            logging.ERROR,
            __file__,
            1,
            "request failed token=%s",
            (secret,),
            sys.exc_info(),
        )

    rendered = _RedactingFormatter(
        "%(levelname)s %(message)s",
        datefmt="%Y-%m-%d",
        secrets=(secret,),
    ).format(record)

    assert secret not in rendered
    assert rendered.count("<redacted>") == 2
