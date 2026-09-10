import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponseForbidden, JsonResponse
from django.urls import reverse, reverse_lazy
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from .forms import CompanyForm
from .models import Company
from tenders.models import OdooCompany
from users.models import CustomUser


def _is_agent(user):
    return getattr(user, 'role', None) == CustomUser.Role.AGENT


def _odoo_companies_for_form():
    return (
        OdooCompany.objects.filter(is_active=True)
        .order_by('name')
        .values_list('id', 'name', 'base_url')
    )


class AgentForbiddenMixin:
    def dispatch(self, request, *args, **kwargs):
        if _is_agent(request.user):
            return HttpResponseForbidden('Agents cannot register companies.')
        return super().dispatch(request, *args, **kwargs)


class CompanyList(AgentForbiddenMixin, LoginRequiredMixin, ListView):
    model = Company
    template_name = 'companies/company_list.html'
    context_object_name = 'companies'

    def get_queryset(self):
        return Company.objects.filter(user=self.request.user)


class CompanyCreate(AgentForbiddenMixin, LoginRequiredMixin, CreateView):
    model = Company
    form_class = CompanyForm
    template_name = 'companies/company_form.html'
    success_url = reverse_lazy('companies:list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['odoo_companies'] = _odoo_companies_for_form()
        context['odoo_companies_json'] = json.dumps([
            {'id': pk, 'label': f'{name} ({base_url})'}
            for pk, name, base_url in context['odoo_companies']
        ])
        return context

    def form_valid(self, form):
        form.instance.user = self.request.user
        messages.success(self.request, 'Company registered successfully.')
        return super().form_valid(form)


class CompanyUpdate(AgentForbiddenMixin, LoginRequiredMixin, UpdateView):
    model = Company
    form_class = CompanyForm
    template_name = 'companies/company_form.html'
    success_url = reverse_lazy('companies:list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['odoo_companies'] = _odoo_companies_for_form()
        context['odoo_companies_json'] = json.dumps([
            {'id': pk, 'label': f'{name} ({base_url})'}
            for pk, name, base_url in context['odoo_companies']
        ])
        return context

    def get_queryset(self):
        return Company.objects.filter(user=self.request.user)


class CompanyDelete(AgentForbiddenMixin, LoginRequiredMixin, DeleteView):
    model = Company
    template_name = 'companies/company_confirm_delete.html'
    success_url = reverse_lazy('companies:list')

    def get_queryset(self):
        return Company.objects.filter(user=self.request.user)


def _json_body(request):
    try:
        return json.loads(request.body or b'{}')
    except (ValueError, TypeError):
        return {}


def _company_dict(company):
    return {
        'id': company.id,
        'name': company.name,
        'registration_number': company.registration_number,
        'tin': company.tin,
        'vat': company.vat,
        'contact_person': company.contact_person,
        'phone': company.phone,
        'email': company.email,
        'address': company.address,
        'city': company.city,
        'country': company.country,
        'odoo_company': company.odoo_company_id,
        'edit_url': reverse('companies:update', args=[company.pk]),
        'delete_url': reverse('companies:delete', args=[company.pk]),
    }


@login_required
def api_company_list(request):
    if _is_agent(request.user):
        return JsonResponse({'ok': False, 'error': 'Agents cannot register companies.'}, status=403)
    companies = Company.objects.filter(user=request.user).order_by('name')
    return JsonResponse({'ok': True, 'companies': [_company_dict(c) for c in companies]})


@login_required
def api_company_save(request, pk=None):
    if _is_agent(request.user):
        return JsonResponse({'ok': False, 'error': 'Agents cannot register companies.'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required.'}, status=405)
    instance = None
    if pk is not None:
        instance = Company.objects.filter(user=request.user, pk=pk).first()
        if instance is None:
            return JsonResponse({'ok': False, 'error': 'Company not found.'})
    form = CompanyForm(_json_body(request), instance=instance)
    if not form.is_valid():
        return JsonResponse({
            'ok': False,
            'error': 'Please fix the highlighted fields.',
            'errors': form.errors,
        })
    company = form.save(commit=False)
    company.user = request.user
    company.save()
    return JsonResponse({'ok': True, 'message': 'Company saved successfully.', 'company': _company_dict(company)})


@login_required
def api_company_delete(request, pk):
    if _is_agent(request.user):
        return JsonResponse({'ok': False, 'error': 'Agents cannot register companies.'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required.'}, status=405)
    company = Company.objects.filter(user=request.user, pk=pk).first()
    if company is None:
        return JsonResponse({'ok': False, 'error': 'Company not found.'})
    name = company.name
    company.delete()
    return JsonResponse({'ok': True, 'message': f'Company {name} deleted successfully.'})