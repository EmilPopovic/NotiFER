import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    def __init__(self):
        super().__init__()
    
    # App components
    @property
    def student_signup_enabled(self) -> bool:
        return os.getenv('STUDENT_SIGNUP', 'false').lower() == 'true'
    
    @property
    def student_pause_enabled(self) -> bool:
        return os.getenv('STUDENT_PAUSE', 'false').lower() == 'true'
    
    @property
    def student_resume_enabled(self) -> bool:
        return os.getenv('STUDENT_RESUME', 'false').lower() == 'true'
    
    @property
    def student_delete_enabled(self) -> bool:
        return os.getenv('STUDENT_DELETE', 'false').lower() == 'true'
    
    @property
    def admin_api_enabled(self) -> bool:
        return os.getenv('ADMIN_API', 'false').lower() == 'true'
    
    @property
    def frontend_enabled(self) -> bool:
        return os.getenv('FRONTEND', 'false').lower() == 'true'
    
    @property
    def allow_query_all_enabled(self) -> bool:
        return os.getenv('ALLOW_QUERY_ALL', 'false').lower() == 'true'
    
    @property
    def data_export_enabled(self) -> bool:
        return os.getenv('DATA_EXPORT', 'true').lower() == 'true'

    @property
    def data_import_enabled(self) -> bool:
        # Off by default: only turned on while migrating onto a new machine.
        return os.getenv('DATA_IMPORT', 'false').lower() == 'true'

    @property
    def notifer_api_token_hash(self) -> str:
        return os.getenv('NOTIFER_API_TOKEN_HASH', '')

    @property
    def recipient_domain(self) -> str:
        return os.getenv('RECIPIENT_DOMAIN', '')
    
    # Worker configuration
    @property
    def worker_enabled(self) -> bool:
        return os.getenv('WORKER', 'false').lower() == 'true'

    @property
    def worker_interval(self) -> int:
        return int(os.getenv('WORKER_INTERVAL', '3600'))
    
    @property
    def max_workers(self) -> int:
        return int(os.getenv('MAX_WORKERS', '10'))

    # Calendar configuration
    @property
    def base_calendar_url(self) -> str:
        return os.getenv('BASE_CALENDAR_URL', 'https://www.fer.unizg.hr/_download/calevent/mycal.ics')

    # Rate Limiting
    @property
    def global_rate_limit(self) -> int:
        return int(os.getenv('GLOBAL_RATE_LIMIT', '15'))
    
    @property
    def global_rate_limit_minutes(self) -> int:
        return int(os.getenv('GLOBAL_RATE_LIMIT_MINUTES', '15'))

    # Email configuration
    @property
    def smtp_server(self) -> str:
        return os.getenv('SMTP_SERVER', '')
    
    @property
    def smtp_port(self) -> int:
        return int(os.getenv('SMTP_PORT', '587'))
    
    @property
    def smtp_username(self) -> str:
        return os.getenv('SMTP_USERNAME', '')
    
    @property
    def smtp_sender_email(self) -> str:
        return os.getenv('SMTP_SENDER_EMAIL', '')
    
    @property
    def smtp_password(self) -> str:
        return os.getenv('SMTP_PASSWORD', '')

    @property
    def email_rate_limit_per_second(self) -> int:
        return int(os.getenv('EMAIL_RATE_LIMIT_PER_SECOND', '2'))
    
    # API configuration
    @property
    def api_url(self) -> str:
        return os.getenv('API_URL', '').replace('${API_PORT}', str(os.getenv('API_PORT', '8026')))
    
    @property
    def api_port(self) -> int:
        return int(os.getenv('API_PORT', '8026'))

    @property
    def dashboard_username(self) -> str:
        return os.getenv('DASHBOARD_USERNAME', '')

    @property
    def dashboard_password_hash(self) -> str:
        return os.getenv('DASHBOARD_PASSWORD_HASH', '')

    class Config:
        env_file = '.env'
        extra = 'ignore'

def get_settings() -> Settings:
    return Settings()
