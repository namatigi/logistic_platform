import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.urls import reverse, reverse_lazy
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from .forms import CompanyForm
from .models import Company


class CompanyList(LoginRequiredMixin, ListView):
    model = Company
    template_name = 'companies/company_list.html'
    context_object_name = 'companies'

    def get_queryset(self):
        return Company.objects.filter(user=self.request.user)


class CompanyCreate(LoginRequiredMixin, CreateView):
    model = Company
    form_class = CompanyForm
    template_name = 'companies/company_form.html'
    success_url = reverse_lazy('companies:list')

    def form_valid(self, form):
        form.instance.user = self.request.user
        messages.success(self.request, 'Company registered successfully.')
        return super().form_valid(form)


class CompanyUpdate(LoginRequiredMixin, UpdateView):
    model = Company
    form_class = CompanyForm
    template_name = 'companies/company_form.html'
    success_url = reverse_lazy('companies:list')

    def get_queryset(self):
        return Company.objects.filter(user=self.request.user)


class CompanyDelete(LoginRequiredMixin, DeleteView):
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
        'contact_person': company.contact_person,
        'phone': company.phone,
        'email': company.email,
        'address': company.address,
        'city': company.city,
        'country': company.country,
        'edit_url': reverse('companies:update', args=[company.pk]),
        'delete_url': reverse('companies:delete', args=[company.pk]),
    }


@login_required
def api_company_list(request):
    companies = Company.objects.filter(user=request.user).order_by('name')
    return JsonResponse({'ok': True, 'companies': [_company_dict(c) for c in companies]})


@login_required
def api_company_save(request, pk=None):
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
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required.'}, status=405)
    company = Company.objects.filter(user=request.user, pk=pk).first()
    if company is None:
        return JsonResponse({'ok': False, 'error': 'Company not found.'})
    name = company.name
    company.delete()
    return JsonResponse({'ok': True, 'message': f'Company {name} deleted successfully.'})