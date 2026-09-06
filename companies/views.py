from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.urls import reverse_lazy
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