from django.core.mail.backends.base import BaseEmailBackend
from django.core.mail.backends.console import EmailBackend as ConsoleBackend
from django.core.mail.backends.smtp import EmailBackend as SMTPBackend


class ApiSettingEmailBackend(BaseEmailBackend):
    def __init__(self, fail_silently=False, **kwargs):
        super().__init__(fail_silently=fail_silently, **kwargs)
        self._backend = None

    def _get_backend(self):
        if self._backend is None:
            from tenders.models import ApiSetting

            setting = ApiSetting.get()
            if setting.smtp_host:
                self._backend = SMTPBackend(
                    host=setting.smtp_host,
                    port=setting.smtp_port,
                    username=setting.smtp_username,
                    password=setting.smtp_password,
                    use_tls=setting.smtp_use_tls,
                    use_ssl=setting.smtp_use_ssl,
                    fail_silently=self.fail_silently,
                )
            else:
                self._backend = ConsoleBackend(fail_silently=self.fail_silently)
        return self._backend

    def open(self):
        return self._get_backend().open()

    def close(self):
        if self._backend is not None:
            self._backend.close()

    def send_messages(self, email_messages):
        return self._get_backend().send_messages(email_messages)