"""
Portable server-migration bundles.

A bundle carries everything NotiFER keeps outside `.env`: the `user_calendars`
and `audit_log` tables plus the cached ICS baselines. It is re-keyed at the
boundary — `calendar_auth` is decrypted with this machine's `ENCRYPTION_KEY`
when the bundle is built and re-encrypted with the target machine's key when it
is applied — so the two deployments never have to share an `ENCRYPTION_KEY`.

The payload is always encrypted under an operator-supplied passphrase. A bundle
is a dump of every subscriber's live FER calendar credential and must never sit
on disk in the clear.

Imports here are relative on purpose. `db_manager` runs as `src.db_manager`
while the API runs with `/app/src` on the path, so an absolute `shared.*` import
would load a second copy of the ORM models bound to a different declarative
Base.
"""

import base64
import datetime
import gzip
import hashlib
import json
import logging
import os

import pytz
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from sqlalchemy import insert, text
from sqlalchemy.orm import Session

from .encryption import get_fernet
from .models import AuditLog, UserCalendar
from .storage_manager import StorageManager

logger = logging.getLogger(__name__)

FORMAT = 'notifer-migration'
VERSION = 1

# The bundle is only as strong as this passphrase, and it protects live credentials.
MIN_PASSPHRASE_LENGTH = 16

# scrypt at n=2**16, r=8 costs ~64 MiB per derivation — fine for an interactive
# admin action, expensive enough to make an offline guess of the passphrase slow.
_SCRYPT_N = 2 ** 16
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16

_LOG_CHUNK = 1000

# Listed explicitly so a schema change that this module does not handle fails
# loudly at export time instead of silently dropping a column.
_USER_COLUMNS = (
    'username',
    'domain',
    'calendar_auth',
    'activated',
    'paused',
    'created',
    'last_checked',
    'last_change_detected',
    'change_count',
    'previous_calendar_path',
    'previous_calendar_hash',
    'language',
)

_LOG_COLUMNS = (
    'id',
    'timestamp',
    'email',
    'action',
    'details',
)

_USER_DATETIMES = ('created', 'last_checked', 'last_change_detected')


class MigrationError(Exception):
    """A recoverable problem building or applying a bundle. Message is operator-facing."""


def _now() -> datetime.datetime:
    # Mirrors crud._now(); duplicated to keep this module free of absolute imports.
    tz = pytz.timezone(os.getenv('TIMEZONE', 'Europe/Zagreb'))
    return datetime.datetime.now(tz=tz).replace(tzinfo=None)


def _iso(value: datetime.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_dt(value, field: str) -> datetime.datetime | None:
    if value is None:
        return None
    try:
        return datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise MigrationError(f'Bundle contains an unparseable timestamp in {field}: {value!r}')


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def _derive_fernet(passphrase: str, salt: bytes, n: int, r: int, p: int) -> Fernet:
    key = Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passphrase.encode('utf-8'))
    return Fernet(base64.urlsafe_b64encode(key))


def _validate_columns(model, expected: tuple[str, ...]) -> None:
    actual = set(model.__table__.columns.keys())
    if actual != set(expected):
        unhandled = sorted(actual - set(expected))
        unknown = sorted(set(expected) - actual)
        raise MigrationError(
            f'Table {model.__tablename__} has drifted from the migration format '
            f'(unhandled columns: {unhandled or "none"}, unknown columns: {unknown or "none"}). '
            'Update shared/migration.py before exporting.'
        )


def _require_passphrase(passphrase: str) -> None:
    if len(passphrase or '') < MIN_PASSPHRASE_LENGTH:
        raise MigrationError(
            f'Passphrase must be at least {MIN_PASSPHRASE_LENGTH} characters.'
        )


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _read_users(db: Session) -> list[dict]:
    """
    Read every subscription with `calendar_auth` decrypted.

    Goes through raw SQL rather than the ORM so that an undecryptable value is
    reported against its own row instead of aborting the whole query from inside
    the EncryptedString type decorator.
    """
    fernet = get_fernet()
    columns = ', '.join(_USER_COLUMNS)
    rows: list[dict] = []
    failures: list[str] = []

    for row in db.execute(text(f'SELECT {columns} FROM user_calendars')).mappings():
        record = dict(row)
        email = f"{record['username']}@{record['domain']}"
        try:
            record['calendar_auth'] = fernet.decrypt(
                record['calendar_auth'].encode('ascii')
            ).decode('utf-8')
        except Exception:
            failures.append(email)
            continue

        for field in _USER_DATETIMES:
            record[field] = _iso(record[field])
        record['activated'] = bool(record['activated'])
        record['paused'] = bool(record['paused'])
        record['change_count'] = int(record['change_count'] or 0)
        rows.append(record)

    if failures:
        shown = ', '.join(failures[:10])
        more = f' (and {len(failures) - 10} more)' if len(failures) > 10 else ''
        raise MigrationError(
            f'Cannot decrypt calendar_auth for {len(failures)} row(s): {shown}{more}. '
            'ENCRYPTION_KEY does not match the data in this database — export aborted '
            'rather than shipping an incomplete bundle.'
        )

    return rows


def _read_audit_log(db: Session) -> list[dict]:
    columns = ', '.join(_LOG_COLUMNS)
    rows = []
    for row in db.execute(text(f'SELECT {columns} FROM audit_log ORDER BY id')).mappings():
        record = dict(row)
        record['timestamp'] = _iso(record['timestamp'])
        rows.append(record)
    return rows


def build_bundle(
        db: Session,
        passphrase: str,
        *,
        include_calendars: bool = True,
        include_audit_log: bool = True,
        include_jwt_key: bool = False,
        storage_manager: StorageManager | None = None,
        api_url: str = '',
) -> tuple[bytes, dict]:
    """
    Build an encrypted migration bundle.

    Returns the file contents and a summary of what went into it. Nothing is
    written to disk — the caller streams the bytes straight to the operator.
    """
    _require_passphrase(passphrase)
    _validate_columns(UserCalendar, _USER_COLUMNS)
    _validate_columns(AuditLog, _LOG_COLUMNS)

    users = _read_users(db)
    logs = _read_audit_log(db) if include_audit_log else []

    calendars: dict[str, str] = {}
    if include_calendars:
        storage = storage_manager or StorageManager()
        for record in users:
            email = f"{record['username']}@{record['domain']}"
            content = storage.get_calendar(email)
            if content is not None:
                calendars[email] = content

    payload: dict = {
        'user_calendars': users,
        'audit_log': logs,
        'calendars': calendars,
    }

    if include_jwt_key:
        jwt_key = os.getenv('JWT_KEY')
        if not jwt_key:
            raise MigrationError('JWT_KEY is not set, so it cannot be included in the bundle.')
        payload['secrets'] = {'JWT_KEY': jwt_key}

    salt = os.urandom(_SALT_BYTES)
    fernet = _derive_fernet(passphrase, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    compressed = gzip.compress(json.dumps(payload).encode('utf-8'), compresslevel=9)
    token = fernet.encrypt(compressed)

    summary = {
        'users': len(users),
        'audit_logs': len(logs),
        'calendars': len(calendars),
        'includes_jwt_key': include_jwt_key,
    }

    envelope = {
        'format': FORMAT,
        'version': VERSION,
        'created': _now().isoformat(),
        'source': {
            'api_url': api_url,
            'timezone': os.getenv('TIMEZONE', 'Europe/Zagreb'),
            'counts': {
                'users': summary['users'],
                'audit_logs': summary['audit_logs'],
                'calendars': summary['calendars'],
            },
            'includes_jwt_key': include_jwt_key,
        },
        'kdf': {
            'algo': 'scrypt',
            'salt': base64.b64encode(salt).decode('ascii'),
            'n': _SCRYPT_N,
            'r': _SCRYPT_R,
            'p': _SCRYPT_P,
        },
        'payload': token.decode('ascii'),
    }

    data = json.dumps(envelope, indent=2).encode('utf-8')
    summary['bytes'] = len(data)
    logger.info(
        f'Built migration bundle: {summary["users"]} user(s), '
        f'{summary["audit_logs"]} log(s), {summary["calendars"]} calendar(s), '
        f'{summary["bytes"]} bytes'
    )
    return data, summary


def create_export_audit_entry(db: Session, summary: dict) -> None:
    """Record that a bundle was produced, and commit it."""
    db.add(AuditLog(
        timestamp=_now(),
        email=None,
        action='data_exported',
        details=(
            f'users={summary["users"]} logs={summary["audit_logs"]} '
            f'calendars={summary["calendars"]} jwt_key={summary["includes_jwt_key"]}'
        ),
    ))
    db.commit()


def bundle_filename(now: datetime.datetime | None = None) -> str:
    stamp = (now or _now()).strftime('%Y%m%d-%H%M%S')
    return f'notifer-export-{stamp}.nfer'


# ---------------------------------------------------------------------------
# Read / decrypt
# ---------------------------------------------------------------------------

def read_envelope(data: bytes) -> dict:
    """
    Parse and validate the cleartext header without touching the payload, so a
    wrong file or an unsupported version fails before a passphrase is needed.
    """
    try:
        envelope = json.loads(data.decode('utf-8'))
    except Exception:
        raise MigrationError('Not a NotiFER migration bundle (the header is unreadable).')

    if not isinstance(envelope, dict) or envelope.get('format') != FORMAT:
        raise MigrationError('Not a NotiFER migration bundle.')

    version = envelope.get('version')
    if version != VERSION:
        raise MigrationError(
            f'Bundle format version {version!r} is not supported by this build (expected {VERSION}).'
        )

    kdf = envelope.get('kdf')
    if not isinstance(kdf, dict) or kdf.get('algo') != 'scrypt' or not kdf.get('salt'):
        raise MigrationError('Bundle header is missing usable key-derivation parameters.')

    if not isinstance(envelope.get('payload'), str):
        raise MigrationError('Bundle header is missing its payload.')

    return envelope


def load_bundle(data: bytes, passphrase: str) -> dict:
    """Decrypt and validate a bundle, returning its payload."""
    envelope = read_envelope(data)
    kdf = envelope['kdf']

    try:
        salt = base64.b64decode(kdf['salt'])
    except Exception:
        raise MigrationError('Bundle header has a malformed key-derivation salt.')

    try:
        fernet = _derive_fernet(
            passphrase,
            salt,
            int(kdf.get('n', _SCRYPT_N)),
            int(kdf.get('r', _SCRYPT_R)),
            int(kdf.get('p', _SCRYPT_P)),
        )
    except (TypeError, ValueError):
        raise MigrationError('Bundle header has malformed key-derivation parameters.')

    try:
        compressed = fernet.decrypt(envelope['payload'].encode('ascii'))
    except InvalidToken:
        raise MigrationError('Wrong passphrase, or the bundle has been altered.')
    except Exception:
        raise MigrationError('Bundle payload could not be decrypted.')

    try:
        payload = json.loads(gzip.decompress(compressed).decode('utf-8'))
    except Exception:
        raise MigrationError('Bundle payload decrypted but could not be read.')

    if not isinstance(payload, dict) or not isinstance(payload.get('user_calendars'), list):
        raise MigrationError('Bundle payload is missing its user_calendars table.')

    payload.setdefault('audit_log', [])
    payload.setdefault('calendars', {})
    payload['envelope'] = envelope
    return payload


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def _prepare_users(payload: dict) -> list[dict]:
    known = set(_USER_COLUMNS)
    seen: set[tuple[str, str]] = set()
    prepared: list[dict] = []
    dropped: set[str] = set()

    for raw in payload['user_calendars']:
        if not isinstance(raw, dict):
            raise MigrationError('Bundle contains a malformed user_calendars entry.')

        dropped.update(set(raw) - known)
        record = {k: v for k, v in raw.items() if k in known}

        for field in ('username', 'domain', 'calendar_auth'):
            if not record.get(field):
                raise MigrationError(f'Bundle contains a user_calendars entry without {field}.')

        key = (record['username'], record['domain'])
        if key in seen:
            raise MigrationError(f'Bundle contains duplicate entries for {key[0]}@{key[1]}.')
        seen.add(key)

        for field in _USER_DATETIMES:
            record[field] = _parse_dt(record.get(field), field)
        record['created'] = record['created'] or _now()
        record['activated'] = bool(record.get('activated'))
        record['paused'] = bool(record.get('paused'))
        record['change_count'] = int(record.get('change_count') or 0)
        record['language'] = record.get('language') or 'hr'
        prepared.append(record)

    if dropped:
        logger.warning(
            f'Ignoring unknown user_calendars field(s) in bundle: {", ".join(sorted(dropped))}'
        )
    return prepared


def _prepare_logs(payload: dict) -> list[dict]:
    known = set(_LOG_COLUMNS)
    prepared = []
    for raw in payload['audit_log']:
        if not isinstance(raw, dict) or not raw.get('action'):
            raise MigrationError('Bundle contains a malformed audit_log entry.')
        record = {k: v for k, v in raw.items() if k in known}
        record['timestamp'] = _parse_dt(record.get('timestamp'), 'audit_log.timestamp') or _now()
        prepared.append(record)
    return prepared


def _resync_audit_log_sequence(db: Session) -> None:
    """
    Audit log rows are restored with their original ids, which leaves the serial
    sequence behind them. Without this the next insert collides on the primary key.
    """
    max_id = db.execute(text('SELECT MAX(id) FROM audit_log')).scalar()
    if max_id:
        db.execute(
            text("SELECT setval(pg_get_serial_sequence('audit_log', 'id'), :value)"),
            {'value': max_id},
        )


def apply_bundle(
        db: Session,
        payload: dict,
        *,
        replace: bool = False,
        dry_run: bool = False,
        storage_manager: StorageManager | None = None,
) -> dict:
    """
    Apply a decrypted bundle to this machine.

    `calendar_auth` is re-encrypted under this machine's ENCRYPTION_KEY by the
    EncryptedString column type. Cached ICS baselines are restored where the
    bundle carries them; where it does not, the baseline is cleared so the first
    worker cycle re-baselines silently instead of leaving a hash with no content
    to compare against.

    Everything happens in one transaction. `dry_run` validates and reports
    without writing anything.
    """
    try:
        get_fernet()
    except RuntimeError as e:
        raise MigrationError(
            f'{e}. Set ENCRYPTION_KEY before importing, or the restored '
            'calendar tokens cannot be encrypted.'
        )

    _validate_columns(UserCalendar, _USER_COLUMNS)
    _validate_columns(AuditLog, _LOG_COLUMNS)

    users = _prepare_users(payload)
    logs = _prepare_logs(payload)
    calendars = payload.get('calendars') or {}

    existing_users = db.execute(text('SELECT COUNT(*) FROM user_calendars')).scalar() or 0
    existing_logs = db.execute(text('SELECT COUNT(*) FROM audit_log')).scalar() or 0

    # A dry run is allowed against a populated database on purpose: validating
    # before committing is the whole point, and the operator needs to be able to
    # do it on the machine they are about to overwrite.
    if existing_users and not replace and not dry_run:
        raise MigrationError(
            f'This database already holds {existing_users} subscription(s). '
            'Re-run with replace enabled to discard them and restore the bundle.'
        )

    restored_calendars = sum(1 for r in users if f"{r['username']}@{r['domain']}" in calendars)

    report = {
        'dry_run': dry_run,
        'existing_users': existing_users,
        'existing_logs': existing_logs,
        'replaced_users': existing_users if (replace and not dry_run) else 0,
        'replaced_logs': existing_logs if (replace and not dry_run) else 0,
        'users': len(users),
        'audit_logs': len(logs),
        'calendars': restored_calendars,
        'rebaselined': len(users) - restored_calendars,
        'includes_jwt_key': bool(payload.get('secrets', {}).get('JWT_KEY')),
    }

    if dry_run:
        logger.info(f'Migration dry run OK: {report}')
        return report

    storage = storage_manager or StorageManager()

    try:
        if replace:
            db.execute(text('DELETE FROM audit_log'))
            db.execute(text('DELETE FROM user_calendars'))
            db.flush()

        for record in users:
            email = f"{record['username']}@{record['domain']}"
            content = calendars.get(email)
            if content is not None:
                path = storage.save_calendar(email, content)
                if not path:
                    raise MigrationError(f'Failed to write the restored calendar for {email}.')
                # Rewrite the path for this machine and recompute the hash from what
                # was actually written; the bundle's path came from another host.
                record['previous_calendar_path'] = path
                record['previous_calendar_hash'] = _sha256(content)
            else:
                record['previous_calendar_path'] = None
                record['previous_calendar_hash'] = None

        db.add_all([UserCalendar(**record) for record in users])
        db.flush()

        for start in range(0, len(logs), _LOG_CHUNK):
            db.execute(insert(AuditLog), logs[start:start + _LOG_CHUNK])
        _resync_audit_log_sequence(db)

        db.add(AuditLog(
            timestamp=_now(),
            email=None,
            action='data_imported',
            details=(
                f'users={report["users"]} logs={report["audit_logs"]} '
                f'calendars={report["calendars"]} rebaselined={report["rebaselined"]}'
            ),
        ))
        db.commit()
    except MigrationError:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception(f'Migration import failed: {e}')
        raise MigrationError(f'Import failed and was rolled back: {e}')

    logger.info(f'Migration import complete: {report}')
    return report


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_encryption(db: Session) -> tuple[int, list[str]]:
    """
    Decrypt every stored `calendar_auth`.

    Returns the number of rows checked and the emails that failed. Catches an
    ENCRYPTION_KEY mismatch immediately instead of leaving it to surface as a
    worker crash an hour later.
    """
    fernet = get_fernet()
    checked = 0
    failures = []

    for row in db.execute(text('SELECT username, domain, calendar_auth FROM user_calendars')).mappings():
        checked += 1
        try:
            fernet.decrypt(row['calendar_auth'].encode('ascii'))
        except Exception:
            failures.append(f"{row['username']}@{row['domain']}")

    return checked, failures
