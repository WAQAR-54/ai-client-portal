"""Opt-in PostgreSQL session timeouts.

CURRENT   production reports statement_timeout=0 and idle_in_transaction_session_timeout=0 (from
          `manage.py ops_verify`): a query, or a transaction someone forgot to close, may run
          forever and pin a connection (the database allows 100; the app holds few).
PROBLEM   a stuck transaction also holds row locks - and the daily-quota lock in
          governance.plans.create_conversation_within_quota is one - so one wedged request could
          block that user's next ones until the connection is dropped.
SOLUTION  two environment variables, both OFF (0) by default, applied as libpq session options:
              DB_IDLE_IN_TRANSACTION_TIMEOUT_MS   safe first step (e.g. 60000): the server drops a
                                                  session idling inside a transaction.
              DB_STATEMENT_TIMEOUT_MS             only after measuring: it also applies to the
                                                  migrations the web container runs at start-up, the
                                                  retention sweep, exports and reports, so a value that
                                                  is too small breaks those.
RISK      none while unset (no behaviour change). When set: a legitimate slow statement is cancelled
          with an error instead of finishing.
ROLLBACK  unset the variable and restart the containers; nothing is stored in the database.
"""


def postgres_timeout_options(statement_timeout_ms=0, idle_in_transaction_timeout_ms=0):
    """The value for DATABASES[...]["OPTIONS"]["options"], or "" when no timeout is requested."""
    parts = []
    if statement_timeout_ms and statement_timeout_ms > 0:
        parts.append(f"-c statement_timeout={int(statement_timeout_ms)}")
    if idle_in_transaction_timeout_ms and idle_in_transaction_timeout_ms > 0:
        parts.append(f"-c idle_in_transaction_session_timeout={int(idle_in_transaction_timeout_ms)}")
    return " ".join(parts)
