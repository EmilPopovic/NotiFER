import getpass
import os
import sys
import logging
from sqlalchemy import MetaData, text
from .shared import migration
from .shared.database import engine, Base, SessionLocal
from .shared.encryption import get_fernet

logger = logging.getLogger(__name__)

def drop_all_tables(force: bool = False):
    from .shared import models  # noqa: F401

    if not force:
        logger.warning('WARNING: This will DELETE ALL DATA in the database!')
        confirmation = input('Type "yes" to confirm: ')
        if confirmation.lower() != 'yes':
            logger.info('Database reset cancelled')
            return
    
    try:
        logger.warning('Dropping all database tables...')
        
        meta = MetaData()
        meta.reflect(bind=engine)
        meta.drop_all(bind=engine)
        
        Base.metadata.drop_all(bind=engine)
        
        logger.info('All tables dropped successfully!')
    except Exception as e:
        logger.error(f'Failed to drop tables: {e}')
        raise

def create_all_tables():
    from .shared import models  # noqa: F401
    
    try:
        logger.info('Creating all database tables...')
        logger.info(f'Registered tables: {list(Base.metadata.tables.keys())}')
        
        Base.metadata.create_all(bind=engine)
        logger.info('All tables created successfully!')

        logger.info('Created tables:')
        for table_name in Base.metadata.tables.keys():
            logger.info(f'  - {table_name}')

    except Exception as e:
        logger.error(f'Failed to create tables: {e}')
        raise

def reset_database(force: bool = False):
    if not force:
        logger.warning('WARNING: This will DELETE ALL DATA in the database!')
        confirmation = input('Type "yes" to confirm: ')
        if confirmation.lower() != 'yes':
            logger.info('Database reset cancelled')
            return
    
    drop_all_tables(force=force)
    create_all_tables()
    logger.info('Database reset complete!')

def check_database():
    from .shared import models  # noqa: F401
    
    try:
        meta = MetaData()
        meta.reflect(bind=engine)
        
        existing_tables = list(meta.tables.keys())
        expected_tables = list(Base.metadata.tables.keys())
        
        if not existing_tables:
            logger.info('Database is NOT initialized - no tables found')
            return False
        
        logger.info('Database is initialized')
        logger.info(f'Found {len(existing_tables)} table(s):')
        for table_name in existing_tables:
            logger.info(f'  - {table_name}')
        
        missing_tables = set(expected_tables) - set(existing_tables)
        if missing_tables:
            logger.warning(f'Missing expected tables: {", ".join(missing_tables)}')
        
        return True
    except Exception as e:
        logger.error(f'Failed to check database: {e}')
        raise

def encrypt_calendar_auth():
    """
    Migrate existing plaintext calendar_auth values to Fernet-encrypted ciphertext.
    Rows that are already encrypted are left unchanged (idempotent).
    """
    fernet = get_fernet()
    migrated = 0
    skipped = 0

    with engine.connect() as conn:
        rows = conn.execute(text('SELECT username, domain, calendar_auth FROM user_calendars')).fetchall()
        for username, domain, raw in rows:
            try:
                # If this succeeds the value is already a valid Fernet token — skip it.
                fernet.decrypt(raw.encode('ascii'))
                skipped += 1
            except Exception:
                # Plaintext — encrypt and update.
                encrypted = fernet.encrypt(raw.encode('utf-8')).decode('ascii')
                conn.execute(
                    text('UPDATE user_calendars SET calendar_auth = :enc WHERE username = :u AND domain = :d'),
                    {'enc': encrypted, 'u': username, 'd': domain}
                )
                migrated += 1
        conn.commit()

    logger.info(f'Encryption migration complete: {migrated} row(s) encrypted, {skipped} already encrypted.')


def _read_passphrase(confirm: bool = False) -> str:
    """
    Read the bundle passphrase from MIGRATION_PASSPHRASE, or prompt for it.

    The env var exists so a migration can be scripted; interactive use should
    prefer the prompt so the passphrase stays out of the shell history.
    """
    passphrase = os.getenv('MIGRATION_PASSPHRASE')
    if passphrase:
        logger.info('Using passphrase from MIGRATION_PASSPHRASE')
        return passphrase

    passphrase = getpass.getpass('Bundle passphrase: ')
    if confirm and passphrase != getpass.getpass('Confirm passphrase: '):
        raise SystemExit('Passphrases do not match.')
    return passphrase


def export_bundle(path: str, include_calendars: bool, include_audit_log: bool, include_jwt_key: bool):
    """Write an encrypted migration bundle to `path`."""
    from .shared import models  # noqa: F401

    if os.path.exists(path):
        raise SystemExit(f'Refusing to overwrite an existing file: {path}')

    passphrase = _read_passphrase(confirm=True)
    session = SessionLocal()
    try:
        data, summary = migration.build_bundle(
            session,
            passphrase,
            include_calendars=include_calendars,
            include_audit_log=include_audit_log,
            include_jwt_key=include_jwt_key,
            api_url=os.getenv('API_URL', ''),
        )
        migration.create_export_audit_entry(session, summary)
    except migration.MigrationError as e:
        raise SystemExit(f'Export failed: {e}')
    finally:
        session.close()

    # 0600: the bundle holds every subscriber's calendar token.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(data)

    logger.info(
        f'Wrote {path}: {summary["users"]} user(s), {summary["audit_logs"]} log(s), '
        f'{summary["calendars"]} calendar(s), {summary["bytes"]} bytes'
    )
    if include_jwt_key:
        logger.warning('This bundle contains JWT_KEY. Treat it as a secrets file.')


def import_bundle(path: str, replace: bool, dry_run: bool):
    """Apply (or validate) a migration bundle from `path`."""
    from .shared import models  # noqa: F401

    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError as e:
        raise SystemExit(f'Cannot read {path}: {e}')

    try:
        envelope = migration.read_envelope(data)
    except migration.MigrationError as e:
        raise SystemExit(f'Import failed: {e}')

    counts = envelope['source']['counts']
    logger.info(
        f'Bundle created {envelope["created"]} on {envelope["source"].get("api_url") or "unknown host"}: '
        f'{counts["users"]} user(s), {counts["audit_logs"]} log(s), {counts["calendars"]} calendar(s)'
    )

    passphrase = _read_passphrase()
    session = SessionLocal()
    try:
        payload = migration.load_bundle(data, passphrase)
        report = migration.apply_bundle(session, payload, replace=replace, dry_run=dry_run)
    except migration.MigrationError as e:
        raise SystemExit(f'Import failed: {e}')
    finally:
        session.close()

    if dry_run:
        logger.info(f'Dry run OK — nothing written. Would restore: {report}')
        if report['existing_users']:
            logger.warning(
                f'This database already holds {report["existing_users"]} subscription(s) and '
                f'{report["existing_logs"]} audit entry/entries. Applying will discard them.'
            )
    else:
        logger.info(f'Import complete: {report}')
        if report['rebaselined']:
            logger.info(
                f'{report["rebaselined"]} subscription(s) have no cached calendar and will '
                're-baseline silently on the first worker cycle (no notification is sent).'
            )
        if report['includes_jwt_key']:
            logger.warning(
                'The bundle carries a JWT_KEY. It was NOT applied — copy it into .env by hand '
                'if links already sent by email must keep working.'
            )
        logger.info('Now run `python -m src.db_manager verify` to confirm the restored tokens decrypt.')


def verify_encryption():
    """Confirm every stored calendar_auth decrypts under the current ENCRYPTION_KEY."""
    from .shared import models  # noqa: F401

    session = SessionLocal()
    try:
        checked, failures = migration.verify_encryption(session)
    finally:
        session.close()

    if not checked:
        logger.info('No subscriptions stored, nothing to verify.')
        return True

    if failures:
        shown = ', '.join(failures[:10])
        more = f' (and {len(failures) - 10} more)' if len(failures) > 10 else ''
        logger.error(
            f'{len(failures)} of {checked} row(s) failed to decrypt: {shown}{more}. '
            'ENCRYPTION_KEY does not match this data.'
        )
        return False

    logger.info(f'All {checked} stored calendar token(s) decrypt correctly.')
    return True


def _usage():
    print('Usage:')
    print('  python -m src.db_manager create          # Create all tables')
    print('  python -m src.db_manager drop            # Drop all tables (with confirmation)')
    print('  python -m src.db_manager drop  --force   # Drop all tables (no confirmation)')
    print('  python -m src.db_manager reset           # Drop and recreate (with confirmation)')
    print('  python -m src.db_manager reset --force   # Drop and recreate (no confirmation)')
    print('  python -m src.db_manager check           # Check if database is initialized')
    print('  python -m src.db_manager encrypt         # Encrypt plaintext calendar_auth values')
    print('  python -m src.db_manager verify          # Check every calendar_auth decrypts')
    print('')
    print('  python -m src.db_manager export <file> [--no-calendars] [--no-audit-log] [--include-jwt-key]')
    print('  python -m src.db_manager import <file> [--replace] [--dry-run]')
    print('')
    print('  Passphrase comes from MIGRATION_PASSPHRASE, or is prompted for.')


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    if len(sys.argv) > 1:
        command = sys.argv[1].lower()
        
        if command == 'create':
            create_all_tables()
        elif command == 'drop':
            force = '--force' in sys.argv
            drop_all_tables(force=force)
        elif command == 'reset':
            force = '--force' in sys.argv
            reset_database(force=force)
        elif command == 'check':
            check_database()
        elif command == 'encrypt':
            encrypt_calendar_auth()
        elif command == 'verify':
            if not verify_encryption():
                sys.exit(1)
        elif command == 'export':
            if len(sys.argv) < 3 or sys.argv[2].startswith('-'):
                print('Usage: python -m src.db_manager export <file> [--no-calendars] [--no-audit-log] [--include-jwt-key]')
                sys.exit(1)
            export_bundle(
                sys.argv[2],
                include_calendars='--no-calendars' not in sys.argv,
                include_audit_log='--no-audit-log' not in sys.argv,
                include_jwt_key='--include-jwt-key' in sys.argv,
            )
        elif command == 'import':
            if len(sys.argv) < 3 or sys.argv[2].startswith('-'):
                print('Usage: python -m src.db_manager import <file> [--replace] [--dry-run]')
                sys.exit(1)
            import_bundle(
                sys.argv[2],
                replace='--replace' in sys.argv,
                dry_run='--dry-run' in sys.argv,
            )
        else:
            _usage()
            sys.exit(1)
    else:
        create_all_tables()

if __name__ == '__main__':
    main()
