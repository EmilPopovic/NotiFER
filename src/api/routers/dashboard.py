import logging
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from shared import maintenance, migration
from shared.database import get_db
from shared.crud import (
    get_all_subscriptions,
    get_subscription,
    get_audit_logs,
    get_audit_log_count,
    get_audit_logs_for_email,
    update_paused,
    delete_user as crud_delete_user,
    create_audit_log,
)
from shared.auth_utils import (
    COOKIE_NAME,
    _SESSION_HOURS,
    verify_password,
    create_session_token,
    verify_session_token,
)
from shared.token_utils import JWT_KEY
from config import get_settings
from api.dependencies import get_storage_manager, get_templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/dashboard', tags=['dashboard'])

_PER_PAGE = 50

# Upper bound on an uploaded bundle, so a bad file cannot exhaust memory.
_MAX_BUNDLE_BYTES = 256 * 1024 * 1024
_ACTION_TYPES = [
    'subscription_created',
    'subscription_resubmit',
    'subscription_activated',
    'subscription_paused',
    'subscription_resumed',
    'subscription_deleted',
    'email_queued',
    'notification_queued',
    'data_exported',
    'data_imported',
]


def _is_authenticated(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME)
    return verify_session_token(token, JWT_KEY) if token else False


def _login_redirect() -> RedirectResponse:
    return RedirectResponse('/dashboard/login', status_code=302)


@router.get('/login', response_class=HTMLResponse)
async def login_page(
    request: Request,
    templates: Jinja2Templates = Depends(get_templates),
):
    if _is_authenticated(request):
        return RedirectResponse('/dashboard/', status_code=302)
    return templates.TemplateResponse('dashboard/login.html', {'request': request})


@router.post('/login')
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    templates: Jinja2Templates = Depends(get_templates),
):
    settings = get_settings()
    if (
        username == settings.dashboard_username
        and verify_password(password, settings.dashboard_password_hash)
    ):
        response = RedirectResponse('/dashboard/', status_code=302)
        response.set_cookie(
            COOKIE_NAME,
            create_session_token(JWT_KEY),
            httponly=True,
            samesite='strict',
            max_age=_SESSION_HOURS * 3600,
        )
        return response
    return templates.TemplateResponse(
        'dashboard/login.html',
        {'request': request, 'error': 'Invalid credentials'},
        status_code=401,
    )


@router.post('/logout')
async def logout():
    response = RedirectResponse('/dashboard/login', status_code=302)
    response.delete_cookie(COOKIE_NAME)
    return response


@router.get('/', response_class=HTMLResponse)
async def dashboard_index(
    request: Request,
    page: int = 1,
    email_filter: str = '',
    action_filter: str = '',
    db: Session = Depends(get_db),
    templates: Jinja2Templates = Depends(get_templates),
):
    if not _is_authenticated(request):
        return _login_redirect()

    subscriptions = get_all_subscriptions(db)

    ef = email_filter.strip() or None
    af = action_filter.strip() or None
    total_logs = get_audit_log_count(db, email=ef, action=af)
    logs = get_audit_logs(db, page=page, per_page=_PER_PAGE, email=ef, action=af)
    total_pages = max(1, (total_logs + _PER_PAGE - 1) // _PER_PAGE)

    stats = {
        'total': len(subscriptions),
        'active': sum(1 for s in subscriptions if s.activated and not s.paused),
        'paused': sum(1 for s in subscriptions if s.activated and s.paused),
        'pending': sum(1 for s in subscriptions if not s.activated),
    }

    return templates.TemplateResponse('dashboard/index.html', {
        'request': request,
        'subscriptions': subscriptions,
        'logs': logs,
        'total_logs': total_logs,
        'page': page,
        'total_pages': total_pages,
        'email_filter': email_filter,
        'action_filter': action_filter,
        'stats': stats,
        'action_types': _ACTION_TYPES,
    })


@router.get('/user', response_class=HTMLResponse)
async def user_detail(
    request: Request,
    email: str,
    db: Session = Depends(get_db),
    templates: Jinja2Templates = Depends(get_templates),
):
    if not _is_authenticated(request):
        return _login_redirect()

    sub = get_subscription(db, email)
    if not sub:
        return RedirectResponse('/dashboard/', status_code=302)

    logs = get_audit_logs_for_email(db, email)

    return templates.TemplateResponse('dashboard/user.html', {
        'request': request,
        'sub': sub,
        'logs': logs,
    })


def _checked(value: str | None) -> bool:
    """An unchecked HTML checkbox is simply absent from the form body."""
    return value is not None


def _reauthenticated(password: str) -> bool:
    """
    Confirm the dashboard password again.

    A migration bundle is every subscriber's live calendar credential, so a
    stolen 8-hour session cookie must not be enough on its own to download one,
    or to overwrite the database with one.
    """
    return verify_password(password, get_settings().dashboard_password_hash)


def _migration_context(request: Request, **extra) -> dict:
    settings = get_settings()
    context = {
        'request': request,
        'export_enabled': settings.data_export_enabled,
        'import_enabled': settings.data_import_enabled,
        'min_passphrase_length': migration.MIN_PASSPHRASE_LENGTH,
        'error': None,
        'export_summary': None,
        'import_report': None,
        'bundle_info': None,
    }
    context.update(extra)
    return context


@router.get('/migration', response_class=HTMLResponse)
async def migration_page(
    request: Request,
    templates: Jinja2Templates = Depends(get_templates),
):
    if not _is_authenticated(request):
        return _login_redirect()
    return templates.TemplateResponse('dashboard/migration.html', _migration_context(request))


@router.post('/export')
async def export_data(
    request: Request,
    password: str = Form(...),
    passphrase: str = Form(...),
    passphrase_confirm: str = Form(...),
    include_calendars: str | None = Form(None),
    include_audit_log: str | None = Form(None),
    include_jwt_key: str | None = Form(None),
    db: Session = Depends(get_db),
    templates: Jinja2Templates = Depends(get_templates),
):
    '''Build an encrypted migration bundle and stream it to the operator.'''
    if not _is_authenticated(request):
        return _login_redirect()

    settings = get_settings()

    def fail(message: str, status_code: int = 400):
        return templates.TemplateResponse(
            'dashboard/migration.html',
            _migration_context(request, error=message),
            status_code=status_code,
        )

    if not settings.data_export_enabled:
        return fail('Data export is disabled (DATA_EXPORT).', 403)

    if not _reauthenticated(password):
        logger.warning('Migration export rejected: dashboard password re-entry failed')
        return fail('Incorrect dashboard password.', 401)

    if passphrase != passphrase_confirm:
        return fail('The two passphrases do not match.')

    try:
        data, summary = migration.build_bundle(
            db,
            passphrase,
            include_calendars=_checked(include_calendars),
            include_audit_log=_checked(include_audit_log),
            include_jwt_key=_checked(include_jwt_key),
            storage_manager=get_storage_manager(),
            api_url=settings.api_url,
        )
    except migration.MigrationError as e:
        logger.error(f'Migration export failed: {e}')
        return fail(str(e))
    except Exception as e:
        logger.exception(f'Migration export failed: {e}')
        return fail('Export failed. Check the server logs.', 500)

    create_audit_log(
        db,
        'data_exported',
        None,
        details=(
            f'users={summary["users"]} logs={summary["audit_logs"]} '
            f'calendars={summary["calendars"]} jwt_key={summary["includes_jwt_key"]}'
        ),
    )
    db.commit()

    filename = migration.bundle_filename()
    logger.info(f'Migration bundle {filename} downloaded ({summary["bytes"]} bytes)')
    return Response(
        content=data,
        media_type='application/octet-stream',
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Cache-Control': 'no-store',
        },
    )


@router.post('/import', response_class=HTMLResponse)
async def import_data(
    request: Request,
    password: str = Form(...),
    passphrase: str = Form(...),
    mode: str = Form('validate'),
    confirm: str = Form(''),
    bundle: UploadFile = File(...),
    db: Session = Depends(get_db),
    templates: Jinja2Templates = Depends(get_templates),
):
    '''Validate or apply a migration bundle uploaded by the operator.'''
    if not _is_authenticated(request):
        return _login_redirect()

    settings = get_settings()

    def render(status_code: int = 200, **extra):
        return templates.TemplateResponse(
            'dashboard/migration.html',
            _migration_context(request, **extra),
            status_code=status_code,
        )

    if not settings.data_import_enabled:
        return render(403, error='Data import is disabled. Set DATA_IMPORT=true to enable it.')

    if not _reauthenticated(password):
        logger.warning('Migration import rejected: dashboard password re-entry failed')
        return render(401, error='Incorrect dashboard password.')

    data = await bundle.read()
    if not data:
        return render(400, error='No bundle file was uploaded.')
    if len(data) > _MAX_BUNDLE_BYTES:
        return render(400, error='Bundle is larger than the 256 MB upload limit; use the CLI importer.')

    dry_run = mode != 'apply'
    replace = confirm.strip().upper() == 'REPLACE'

    if not dry_run and not replace:
        return render(400, error='Type REPLACE to confirm that existing data may be discarded.')

    try:
        payload = migration.load_bundle(data, passphrase)
    except migration.MigrationError as e:
        logger.error(f'Migration import rejected: {e}')
        return render(400, error=str(e))

    info = payload['envelope']

    # Hold the worker off so it does not poll against rows being rewritten.
    maintenance.pause()
    try:
        if not dry_run and not maintenance.wait_for_idle(timeout=90):
            return render(
                409,
                error='A worker cycle is still running. Wait for it to finish and retry.',
                bundle_info=info,
            )
        report = migration.apply_bundle(
            db,
            payload,
            replace=replace,
            dry_run=dry_run,
            storage_manager=get_storage_manager(),
        )
    except migration.MigrationError as e:
        logger.error(f'Migration import failed: {e}')
        return render(400, error=str(e), bundle_info=info)
    except Exception as e:
        logger.exception(f'Migration import failed: {e}')
        return render(500, error='Import failed. Check the server logs.', bundle_info=info)
    finally:
        maintenance.resume()

    logger.info(f'Migration import {"validated" if dry_run else "applied"}: {report}')
    return render(import_report=report, bundle_info=info)


@router.post('/action')
async def perform_action(
    request: Request,
    email: str = Form(...),
    action: str = Form(...),
    next_url: str = Form('/dashboard/'),
    db: Session = Depends(get_db),
):
    if not _is_authenticated(request):
        return _login_redirect()

    if not next_url.startswith('/dashboard'):
        next_url = '/dashboard/'

    if action == 'pause':
        create_audit_log(db, 'subscription_paused', email, details='admin_dashboard')
        update_paused(db, email, True)
    elif action == 'unpause':
        create_audit_log(db, 'subscription_resumed', email, details='admin_dashboard')
        update_paused(db, email, False)
    elif action == 'delete':
        create_audit_log(db, 'subscription_deleted', email, details='admin_dashboard')
        crud_delete_user(db, email)
        next_url = '/dashboard/'

    return RedirectResponse(next_url, status_code=302)
