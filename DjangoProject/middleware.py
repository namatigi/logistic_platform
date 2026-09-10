import json


class ApiDiagnosticMiddleware:
    """Record error responses produced by the platform's API points.

    Any response that walks like an API/webhook response and is an error
    (HTTP 4xx/5xx or a JSON body with ``"ok": false``) is persisted to
    :class:`tenders.models.ApiDiagnostic` along with the calling account,
    the timestamp and the error message.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self._api_path_prefixes = ('/api/', '/webhook/')

    def _is_api_request(self, path):
        return path.startswith(self._api_path_prefixes)

    def _extract_message(self, payload, status_code):
        if isinstance(payload, dict):
            for key in ('error', 'detail', 'message'):
                value = payload.get(key)
                if value:
                    if isinstance(value, (dict, list)):
                        return json.dumps(value)
                    return str(value)
        from http import HTTPStatus
        try:
            return HTTPStatus(status_code).phrase
        except ValueError:
            return f'HTTP {status_code}'

    def __call__(self, request):
        if self._is_api_request(request.path):
            return self._process(request)
        return self.get_response(request)

    def _process(self, request):
        response = self.get_response(request)
        if getattr(request, '_api_diagnostic_logged', False):
            return response

        content_type = (response.get('Content-Type') or '').lower()
        is_json = 'application/json' in content_type
        is_error = response.status_code >= 400

        payload = None
        if is_json:
            try:
                payload = json.loads(response.content or b'{}')
            except (ValueError, TypeError):
                payload = None

        if payload is not None and isinstance(payload, dict) and payload.get('ok') is False:
            is_error = True

        if not is_error:
            return response

        from tenders.models import ApiDiagnostic

        api_point = getattr(request.resolver_match, 'url_name', None)
        if not api_point:
            api_point = request.path
        if api_point.startswith('api_'):
            api_point = api_point[len('api_'):]
        elif api_point.startswith('webhook_'):
            api_point = api_point[len('webhook_'):]

        user = request.user if getattr(request.user, 'is_authenticated', False) else None
        try:
            ApiDiagnostic.objects.create(
                user=user,
                api_point=api_point,
                method=request.method,
                path=request.path,
                status_code=response.status_code,
                message=self._extract_message(payload, response.status_code),
                detail=payload if is_json else None,
            )
        except Exception:
            pass
        return response